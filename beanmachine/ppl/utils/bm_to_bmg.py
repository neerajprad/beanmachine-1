#!/usr/bin/env python3
"""Tools to transform Bean Machine programs to Bean Machine Graph"""

import ast
from typing import List

import astor
from beanmachine.ppl.utils.ast_patterns import (
    arguments,
    assign,
    ast_domain,
    ast_return,
    binop,
    bool_constant,
    call_to,
    constant_numeric,
    constant_tensor_any,
    function_def,
    module,
    name,
    unaryop,
)
from beanmachine.ppl.utils.fold_constants import _fold_unary_op, fold
from beanmachine.ppl.utils.patterns import ListAny
from beanmachine.ppl.utils.rules import (
    AllListMembers,
    AllOf as all_of,
    FirstMatch as first,
    ListEdit,
    PatternRule,
    SomeListMembers,
    TryMany as many,
    TryOnce as once,
    if_then,
    projection_rule,
)
from beanmachine.ppl.utils.single_assignment import single_assignment


# TODO: Remove the sample annotations
# TODO: Memoize each sample method
# TODO: Detect unsupported operators
# TODO: Detect unsupported control flow
# TODO: Would be helpful if we could track original source code locations.
# TODO: Transform y**x into exp(x*log(y))
# TODO: How to handle division? Turn x/y into x * exp(-log(y))?
# TODO: Collapse adds and multiplies in the graph
# TODO: Are we imposing a restriction that a sample method always returns
# TODO: a distribution? How will we represent things like
# TODO: @sample def x(): return Bern(0.5) + Bern(0.5)
# TODO: ?

_top_down = ast_domain.top_down
_bottom_up = ast_domain.bottom_up
_descend_until = ast_domain.descend_until
_specific_child = ast_domain.specific_child

_eliminate_subtraction = PatternRule(
    binop(op=ast.Sub),
    lambda b: ast.BinOp(
        left=b.left, op=ast.Add(), right=ast.UnaryOp(op=ast.USub(), operand=b.right)
    ),
)

_fix_usub_usub = PatternRule(
    unaryop(op=ast.USub, operand=unaryop(op=ast.USub)), lambda u: u.operand.operand
)

_fold_usub_const = PatternRule(
    unaryop(op=ast.USub, operand=constant_numeric), _fold_unary_op
)


_fix_arithmetic = all_of(
    [
        _top_down(once(_eliminate_subtraction)),
        _bottom_up(many(first([_fix_usub_usub, _fold_usub_const]))),
    ]
)


_bmg = ast.Name(id="bmg", ctx=ast.Load())


def _make_bmg_call(name: str, args: List[ast.AST]) -> ast.AST:
    return ast.Call(func=ast.Attribute(value=_bmg, attr=name), args=args, keywords=[])


_add_boolean = PatternRule(
    assign(value=bool_constant),
    lambda a: ast.Assign(a.targets, _make_bmg_call("add_boolean", [a.value])),
)

_add_real = PatternRule(
    assign(value=ast.Num),
    lambda a: ast.Assign(a.targets, _make_bmg_call("add_real", [a.value])),
)

_add_tensor = PatternRule(
    assign(value=constant_tensor_any),
    lambda a: ast.Assign(a.targets, _make_bmg_call("add_tensor", [a.value])),
)

_add_negate = PatternRule(
    assign(value=unaryop(op=ast.USub)),
    lambda a: ast.Assign(a.targets, _make_bmg_call("add_negate", [a.value.operand])),
)

_add_addition = PatternRule(
    assign(value=binop(op=ast.Add)),
    lambda a: ast.Assign(
        a.targets, _make_bmg_call("add_addition", [a.value.left, a.value.right])
    ),
)

_add_multiplication = PatternRule(
    assign(value=binop(op=ast.Mult)),
    lambda a: ast.Assign(
        a.targets, _make_bmg_call("add_multiplication", [a.value.left, a.value.right])
    ),
)

_add_exp = PatternRule(
    assign(value=call_to(id="exp")),
    lambda a: ast.Assign(a.targets, _make_bmg_call("add_exp", [a.value.args[0]])),
)

_add_bernoulli = PatternRule(
    assign(value=call_to(id="Bernoulli")),
    lambda a: ast.Assign(a.targets, _make_bmg_call("add_bernoulli", [a.value.args[0]])),
)

_add_sample = PatternRule(
    ast_return(), lambda r: ast.Return(value=_make_bmg_call("add_sample", [r.value]))
)

# TODO: add_to_real
# TODO: add_observation

_math_to_bmg = _top_down(
    once(
        first(
            [
                _add_boolean,
                _add_real,
                _add_tensor,
                _add_negate,
                _add_addition,
                _add_multiplication,
                _add_exp,
                _add_bernoulli,
            ]
        )
    )
)


_is_sample: PatternRule = PatternRule(
    function_def(decorator_list=ListAny(name(id="sample")))
)

_no_params: PatternRule = PatternRule(function_def(args=arguments(args=[])))

_returns_to_bmg = _descend_until(_is_sample, _top_down(once(_add_sample)))

_sample_to_memoize = _descend_until(
    _is_sample,
    _specific_child(
        "decorator_list",
        SomeListMembers(
            PatternRule(name(id="sample"), lambda n: ast.Name(id="memoize"))
        ),
    ),
)

_header = ast.parse(
    """
from beanmachine.ppl.utils.memoize import memoize
from beanmachine.ppl.utils.bm_graph_builder import BMGraphBuilder
bmg = BMGraphBuilder()"""
)


def _prepend_statements(module: ast.Module, statements: List[ast.stmt]) -> ast.Module:
    return ast.Module(body=statements + module.body)


def _append_statements(module: ast.Module, statements: List[ast.stmt]) -> ast.Module:
    return ast.Module(body=module.body + statements)


_samples_to_calls = AllListMembers(
    if_then(
        all_of([_is_sample, _no_params]),
        projection_rule(
            lambda f: ast.Expr(
                value=ast.Call(
                    func=ast.Name(id=f.name, ctx=ast.Load()), args=[], keywords=[]
                )
            )
        ),
        projection_rule(lambda f: ListEdit([])),
    )
)

_to_bmg = all_of([_math_to_bmg, _returns_to_bmg, _sample_to_memoize])


def to_python(source: str) -> str:
    a: ast.Module = ast.parse(source)
    f = fold(a)
    assert isinstance(f, ast.Module)
    # The AST has now had constants folded and associative
    # operators are nested to the left.
    es = _fix_arithmetic(f).expect_success()
    assert isinstance(es, ast.Module)
    # The AST has now eliminated all subtractions; negative constants
    # are represented as constants, not as USubs
    sa = single_assignment(es)
    assert isinstance(sa, ast.Module)
    # Now we're in single assignment form.
    bmg = _to_bmg(sa).expect_success()
    assert isinstance(bmg, ast.Module)
    bmg = _prepend_statements(bmg, _header.body)
    assert isinstance(bmg, ast.Module)
    footer = _samples_to_calls(a.body).expect_success()
    bmg = _append_statements(bmg, footer)
    # TODO: Fix negative constants back to standard form.
    p: str = astor.to_source(bmg)
    return p