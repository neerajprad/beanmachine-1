# Copyright (c) Facebook, Inc. and its affiliates.
# TODO: Update this comment
"""A builder for the BeanMachine Graph language

The Beanstalk compiler has, at a high level, five phases.

* First, it transforms a Python model into a semantically
  equivalent program "single assignment" (SA) form that uses
  only a small subset of Python features.

* Second, it transforms that program into a "lifted" form.
  Portions of the program which do not involve samples are
  executed normally, but any computation that involves a
  stochastic node in any way is instead turned into a graph node.

  Jargon note:

  Every graph of a model will have some nodes that represent
  random samples and some which do not. For instance,
  we might have a simple coin flip model with three
  nodes: a sample, a distribution, and a constant probability:

  def flip():
    return Bernoulli(0.5)

  sample --> Bernoulli --> 0.5

  We'll refer to the nodes which somehow involve a sample,
  either directly or indirectly, as "stochastic" nodes.

* Third, we actually execute the lifted program and
  accumulate the graph.

* Fourth, the accumulated graph tracks the type information
  of the original Python program. We mutate the accumulated
  graph into a form where it obeys the rules of the BMG
  type system.

* Fifth, we either create actual BMG nodes in memory via
  native code interop, or we emit a program in Python or
  C++ which does so.

This module implements the graph builder that is called during
execution of the lifted program; it implements phases
three, four and five.
"""

import inspect
import math
from types import MethodType
from typing import Any, Callable, Dict, List, Optional, Set

import beanmachine.ppl.compiler.bmg_nodes as bn
import beanmachine.ppl.compiler.profiler as prof
import torch
import torch.distributions as dist
from beanmachine.ppl.compiler.beanstalk_common import allowed_functions
from beanmachine.ppl.compiler.bm_graph_builder import BMGraphBuilder, phi
from beanmachine.ppl.compiler.bmg_nodes import BMGNode, ConstantNode
from beanmachine.ppl.compiler.hint import log1mexp, math_log1mexp
from beanmachine.ppl.inference.abstract_infer import _verify_queries_and_observations
from beanmachine.ppl.model.rv_identifier import RVIdentifier
from beanmachine.ppl.utils.memoize import MemoizationKey


def _hashable(x: Any) -> bool:
    # Oddly enough, Python does not allow you to test for set inclusion
    # if the object is not hashable. Since it is impossible for an unhashable
    # object to be in a set, Python could simply say no when asked if a set
    # contains any unhashable object. It does not, so we are forced to do so.

    # All hashable objects have a callable __hash__ attribute.
    if not hasattr(x, "__hash__"):
        return False
    if not isinstance(x.__hash__, Callable):
        return False

    # It is possible that callable __hash__ exists but throws, which makes it
    # unhashable. Eliminate that possibility as well.
    try:
        hash(x)
    except Exception:
        return False

    return True


def _flatten_all_lists(xs):
    """Takes a list-of-lists, with arbitrary nesting level;
    returns an iteration of all elements."""
    if isinstance(xs, list):
        for x in xs:
            yield from _flatten_all_lists(x)
    else:
        yield xs


def _list_to_zeros(xs):
    """Takes a list-of-lists, with arbitrary nesting level;
    returns a list-of-lists of the same shape but with every non-list
    element replaced with zero."""
    if isinstance(xs, list):
        return [_list_to_zeros(x) for x in xs]
    return 0


builtin_function_or_method = type(abs)

# If we encounter a function call on a stochastic node during
# graph accumulation we will attempt to build a node in the graph
# for that invocation. These are the instance methods on tensors
# that we recognize and can build a graph node for.  See
# KnownFunction below for more details.

known_tensor_instance_functions = [
    "add",
    "div",
    "exp",
    "expm1",
    "float",
    "int",
    "item",
    "log",
    "logical_not",
    "logsumexp",
    "mm",
    "mul",
    "neg",
    "pow",
    "sigmoid",
    "sub",
]


# This helper class is to solve a problem in the simulated
# execution of the model during graph accumulation. Consider
# a model fragment like:
#
#   n = normal()
#   y = n.exp()
#
# During graph construction, n will be a SampleNode whose
# operand is a NormalNode, but SampleNode does not have a
# method "exp".
#
# The lifted program we execute will be something like:
#
#   n = bmg.handle_function(normal, [])
#   func = bmg.handle_dot(n, "exp")
#   y = bmg.handle_function(func, [])
#
# The "func" that is returned is one of these KnownFunction
# objects, which captures the notion "I am an invocation
# of known function Tensor.exp on a receiver that is a BMG
# node".  We then turn that into a exp node in handle_function.


class KnownFunction:
    receiver: BMGNode
    function: Callable

    def __init__(self, receiver: BMGNode, function: Callable) -> None:
        if not isinstance(receiver, BMGNode):
            raise TypeError(
                f"KnownFunction receiver must be BMGNode but is {type(receiver)}"
            )
        if not isinstance(function, Callable):
            raise TypeError(
                f"KnownFunction function must be Callable but is {type(function)}"
            )

        self.receiver = receiver
        self.function = function


def only_ordinary_arguments(args, kwargs) -> bool:
    if any(isinstance(arg, BMGNode) for arg in args):
        return False
    if any(isinstance(arg, BMGNode) for arg in kwargs.values()):
        return False
    return True


def _is_random_variable_call(f) -> bool:
    return hasattr(f, "is_random_variable") and f.is_random_variable


def _is_functional_call(f) -> bool:
    return hasattr(f, "is_functional") and f.is_functional


def _is_phi(f: Any, arguments: List[Any], kwargs: Dict[str, Any]) -> bool:
    # We need to know if this call is Normal.cdf(Normal(0.0, 1.0), x).
    # (Note that we have already rewritten Normal(0.0, 1.0).cdf(x) into
    # this form.)
    # TODO: Support kwargs
    if f is not dist.Normal.cdf or len(arguments) < 2:
        return False
    s = arguments[0]
    return isinstance(s, dist.Normal) and s.mean == 0.0 and s.stddev == 1.0


def _has_source_code(function: Callable) -> bool:
    try:
        inspect.getsource(function)
    except Exception:
        return False
    return True


class BMGRuntime:

    _bmg: BMGraphBuilder

    # As we execute the lifted program, we accumulate graph nodes in the
    # graph builder,and the program passes around graph nodes instead of
    # regular values. What happens when a graph node is passed to a
    # function, or used as the receiver of a function? That function will be
    # expecting a regular value as its argument or receiver.
    #
    # Certain function calls are special because they call graph nodes to
    # be created; we have a dictionary here that maps Python function objects
    # to the graph builder method that knows how to create the appropriate
    # node type.
    #
    # There are also some functions which we know can be passed a graph node
    # and will treat it correctly even though it is a graph node and not
    # a value. For example, the function which constructs a dictionary
    # or the function which constructs a list. When we encounter one of
    # these functions in the lifted program, we do not create a graph node
    # or call a special helper function; we simply allow it to be called normally.

    function_map: Dict[Callable, Callable]

    # As we construct the graph we may encounter "random variable" values; these
    # refer to a function that we need to transform into the "lifted" form. This
    # map tracks those so that we do not repeat work. However, RVIDs contain a
    # tuple of arguments which might contain tensors, and tensors are hashed by
    # reference, not by value. We therefore construct a map of RVID-equivalents
    # which is hashable by the values of the arguments.

    rv_map: Dict[MemoizationKey, BMGNode]
    lifted_map: Dict[Callable, Callable]

    # The graph we accumulate must be acyclic. We assume that an RVID-returning
    # function is pure, so if at any time such a function calls itself, either
    # it is impure or it is in an infinite recursion; either way, we will not
    # be able to construct a correct graph. When we are calling the lifted
    # form of a functional or random_variable method we track the RVID that
    # was used to trigger the call; if we ever encounter a call with the same
    # RVID while the lifted execution is "in flight", we throw an exception
    # and stop accumulating the graph.

    in_flight: Set[MemoizationKey]

    # We also need to keep track of which query node is associated
    # with each RVID queried by the user. Query nodes are deduplicated
    # so it is possible that two different RVIDs are associated with
    # the same query node.

    _rv_to_query: Dict[RVIdentifier, bn.Query]

    _pd: Optional[prof.ProfilerData]

    def __init__(self) -> None:
        self._bmg = BMGraphBuilder()
        self._pd = None
        self.rv_map = {}
        self.lifted_map = {}
        self.in_flight = set()
        self._rv_to_query = {}
        self.function_map = {
            # Math functions
            math.exp: self.handle_exp,
            math.log: self.handle_log,
            # Tensor instance functions
            torch.Tensor.add: self.handle_addition,  # pyre-ignore
            torch.Tensor.div: self.handle_division,
            torch.Tensor.exp: self.handle_exp,  # pyre-ignore
            torch.Tensor.expm1: self.handle_expm1,  # pyre-ignore
            torch.Tensor.float: self.handle_to_real,
            torch.Tensor.item: self.handle_item,
            torch.Tensor.int: self.handle_to_int,
            torch.Tensor.logical_not: self.handle_not,  # pyre-ignore
            torch.Tensor.log: self.handle_log,
            torch.Tensor.logsumexp: self.handle_logsumexp,
            torch.Tensor.mm: self.handle_matrix_multiplication,  # pyre-ignore
            torch.Tensor.mul: self.handle_multiplication,
            torch.Tensor.neg: self.handle_negate,
            torch.Tensor.pow: self.handle_power,
            torch.Tensor.sigmoid: self.handle_logistic,
            torch.Tensor.sub: self.handle_subtraction,
            # Tensor static functions
            torch.add: self.handle_addition,
            torch.div: self.handle_division,
            torch.exp: self.handle_exp,
            torch.expm1: self.handle_expm1,
            # Note that torch.float is not a function.
            torch.log: self.handle_log,
            torch.logsumexp: self.handle_logsumexp,
            torch.logical_not: self.handle_not,
            torch.mm: self.handle_matrix_multiplication,
            torch.mul: self.handle_multiplication,
            torch.neg: self.handle_negate,
            torch.pow: self.handle_power,
            torch.sigmoid: self.handle_logistic,
            torch.sub: self.handle_subtraction,
            # Distribution constructors
            dist.Bernoulli: self.handle_bernoulli,
            dist.Beta: self.handle_beta,
            dist.Binomial: self.handle_binomial,
            dist.Categorical: self.handle_categorical,
            dist.Dirichlet: self.handle_dirichlet,
            dist.Chi2: self.handle_chi2,
            dist.Gamma: self.handle_gamma,
            dist.HalfCauchy: self.handle_halfcauchy,
            dist.Normal: self.handle_normal,
            dist.HalfNormal: self.handle_halfnormal,
            dist.StudentT: self.handle_studentt,
            dist.Uniform: self.handle_uniform,
            # Beanstalk hints
            log1mexp: self.handle_log1mexp,
            math_log1mexp: self.handle_log1mexp,
        }

    def _begin(self, s: str) -> None:
        pd = self._pd
        if pd is not None:
            pd.begin(s)

    def _finish(self, s: str) -> None:
        pd = self._pd
        if pd is not None:
            pd.finish(s)

    def handle_bernoulli(
        self, probs: Any = None, logits: Any = None
    ) -> bn.BernoulliNode:
        if (probs is None and logits is None) or (
            probs is not None and logits is not None
        ):
            raise ValueError("handle_bernoulli requires exactly one of probs or logits")
        probability = logits if probs is None else probs
        if not isinstance(probability, BMGNode):
            probability = self._bmg.add_constant(probability)
        if logits is None:
            return self._bmg.add_bernoulli(probability)
        return self._bmg.add_bernoulli_logit(probability)

    # TODO: Add a note here describing why it is important that the function
    # signatures of the handler methods for distributions match those of
    # the torch distribution constructors.

    # TODO: Verify that they do match.

    def handle_binomial(
        self, total_count: Any, probs: Any = None, logits: Any = None
    ) -> bn.BinomialNode:
        if (probs is None and logits is None) or (
            probs is not None and logits is not None
        ):
            raise ValueError("handle_binomial requires exactly one of probs or logits")
        probability = logits if probs is None else probs
        if not isinstance(total_count, BMGNode):
            total_count = self._bmg.add_constant(total_count)
        if not isinstance(probability, BMGNode):
            probability = self._bmg.add_constant(probability)
        if logits is None:
            return self._bmg.add_binomial(total_count, probability)
        return self._bmg.add_binomial_logit(total_count, probability)

    def handle_categorical(
        self, probs: Any = None, logits: Any = None
    ) -> bn.CategoricalNode:
        if (probs is None and logits is None) or (
            probs is not None and logits is not None
        ):
            raise ValueError(
                "handle_categorical requires exactly one of probs or logits"
            )
        probability = logits if probs is None else probs
        if not isinstance(probability, BMGNode):
            probability = self._bmg.add_constant(probability)
        return self._bmg.add_categorical(probability, logits is not None)

    def handle_chi2(self, df: Any, validate_args=None) -> bn.Chi2Node:
        if not isinstance(df, BMGNode):
            df = self._bmg.add_constant(df)
        return self._bmg.add_chi2(df)

    def handle_gamma(
        self, concentration: Any, rate: Any, validate_args=None
    ) -> bn.GammaNode:
        if not isinstance(concentration, BMGNode):
            concentration = self._bmg.add_constant(concentration)
        if not isinstance(rate, BMGNode):
            rate = self._bmg.add_constant(rate)
        return self._bmg.add_gamma(concentration, rate)

    def handle_halfcauchy(self, scale: Any, validate_args=None) -> bn.HalfCauchyNode:
        if not isinstance(scale, BMGNode):
            scale = self._bmg.add_constant(scale)
        return self._bmg.add_halfcauchy(scale)

    def handle_normal(self, loc: Any, scale: Any, validate_args=None) -> bn.NormalNode:
        if not isinstance(loc, BMGNode):
            loc = self._bmg.add_constant(loc)
        if not isinstance(scale, BMGNode):
            scale = self._bmg.add_constant(scale)
        return self._bmg.add_normal(loc, scale)

    def handle_halfnormal(self, scale: Any, validate_args=None) -> bn.HalfNormalNode:
        if not isinstance(scale, BMGNode):
            scale = self._bmg.add_constant(scale)
        return self._bmg.add_halfnormal(scale)

    def handle_dirichlet(
        self, concentration: Any, validate_args=None
    ) -> bn.DirichletNode:
        if not isinstance(concentration, BMGNode):
            concentration = self._bmg.add_constant(concentration)
        return self._bmg.add_dirichlet(concentration)

    def handle_studentt(
        self, df: Any, loc: Any = 0.0, scale: Any = 1.0, validate_args=None
    ) -> bn.StudentTNode:
        if not isinstance(df, BMGNode):
            df = self._bmg.add_constant(df)
        if not isinstance(loc, BMGNode):
            loc = self._bmg.add_constant(loc)
        if not isinstance(scale, BMGNode):
            scale = self._bmg.add_constant(scale)
        return self._bmg.add_studentt(df, loc, scale)

    def handle_uniform(self, low: Any, high: Any, validate_args=None) -> bn.UniformNode:
        if not isinstance(low, BMGNode):
            low = self._bmg.add_constant(low)
        if not isinstance(high, BMGNode):
            high = self._bmg.add_constant(high)
        return self._bmg.add_uniform(low, high)

    def handle_beta(
        self, concentration1: Any, concentration0: Any, validate_args=None
    ) -> bn.BetaNode:
        if not isinstance(concentration1, BMGNode):
            concentration1 = self._bmg.add_constant(concentration1)
        if not isinstance(concentration0, BMGNode):
            concentration0 = self._bmg.add_constant(concentration0)
        return self._bmg.add_beta(concentration1, concentration0)

    def handle_poisson(
        self,
        rate: Any,
    ) -> bn.PoissonNode:
        if not isinstance(rate, BMGNode):
            rate = self._bmg.add_constant(rate)
        return self._bmg.add_poisson(rate)

    def handle_greater_than(self, input: Any, other: Any) -> Any:
        if (not isinstance(input, BMGNode)) and (not isinstance(other, BMGNode)):
            return input > other
        if not isinstance(input, BMGNode):
            input = self._bmg.add_constant(input)
        if not isinstance(other, BMGNode):
            other = self._bmg.add_constant(other)
        if isinstance(input, ConstantNode) and isinstance(other, ConstantNode):
            return input.value > other.value
        return self._bmg.add_greater_than(input, other)

    def handle_greater_than_equal(self, input: Any, other: Any) -> Any:
        if (not isinstance(input, BMGNode)) and (not isinstance(other, BMGNode)):
            return input >= other
        if not isinstance(input, BMGNode):
            input = self._bmg.add_constant(input)
        if not isinstance(other, BMGNode):
            other = self._bmg.add_constant(other)
        if isinstance(input, ConstantNode) and isinstance(other, ConstantNode):
            return input.value >= other.value
        return self._bmg.add_greater_than_equal(input, other)

    def handle_less_than(self, input: Any, other: Any) -> Any:
        if (not isinstance(input, BMGNode)) and (not isinstance(other, BMGNode)):
            return input < other
        if not isinstance(input, BMGNode):
            input = self._bmg.add_constant(input)
        if not isinstance(other, BMGNode):
            other = self._bmg.add_constant(other)
        if isinstance(input, ConstantNode) and isinstance(other, ConstantNode):
            return input.value < other.value
        return self._bmg.add_less_than(input, other)

    def handle_less_than_equal(self, input: Any, other: Any) -> Any:
        if (not isinstance(input, BMGNode)) and (not isinstance(other, BMGNode)):
            return input <= other
        if not isinstance(input, BMGNode):
            input = self._bmg.add_constant(input)
        if not isinstance(other, BMGNode):
            other = self._bmg.add_constant(other)
        if isinstance(input, ConstantNode) and isinstance(other, ConstantNode):
            return input.value <= other.value
        return self._bmg.add_less_than_equal(input, other)

    def handle_equal(self, input: Any, other: Any) -> Any:
        if (not isinstance(input, BMGNode)) and (not isinstance(other, BMGNode)):
            return input == other
        if not isinstance(input, BMGNode):
            input = self._bmg.add_constant(input)
        if not isinstance(other, BMGNode):
            other = self._bmg.add_constant(other)
        if isinstance(input, ConstantNode) and isinstance(other, ConstantNode):
            return input.value == other.value
        return self._bmg.add_equal(input, other)

    def handle_not_equal(self, input: Any, other: Any) -> Any:
        if (not isinstance(input, BMGNode)) and (not isinstance(other, BMGNode)):
            return input != other
        if not isinstance(input, BMGNode):
            input = self._bmg.add_constant(input)
        if not isinstance(other, BMGNode):
            other = self._bmg.add_constant(other)
        if isinstance(input, ConstantNode) and isinstance(other, ConstantNode):
            return input.value != other.value
        return self._bmg.add_not_equal(input, other)

    def handle_is(self, input: Any, other: Any) -> Any:
        if (not isinstance(input, BMGNode)) and (not isinstance(other, BMGNode)):
            return input is other
        if not isinstance(input, BMGNode):
            input = self._bmg.add_constant(input)
        if not isinstance(other, BMGNode):
            other = self._bmg.add_constant(other)
        return self._bmg.add_is(input, other)

    def handle_is_not(self, input: Any, other: Any) -> Any:
        if (not isinstance(input, BMGNode)) and (not isinstance(other, BMGNode)):
            return input is not other
        if not isinstance(input, BMGNode):
            input = self._bmg.add_constant(input)
        if not isinstance(other, BMGNode):
            other = self._bmg.add_constant(other)
        return self._bmg.add_is_not(input, other)

    def handle_in(self, input: Any, other: Any) -> Any:
        if (not isinstance(input, BMGNode)) and (not isinstance(other, BMGNode)):
            return input in other
        if not isinstance(input, BMGNode):
            input = self._bmg.add_constant(input)
        if not isinstance(other, BMGNode):
            other = self._bmg.add_constant(other)
        return self._bmg.add_in(input, other)

    def handle_not_in(self, input: Any, other: Any) -> Any:
        if (not isinstance(input, BMGNode)) and (not isinstance(other, BMGNode)):
            return input in other
        if not isinstance(input, BMGNode):
            input = self._bmg.add_constant(input)
        if not isinstance(other, BMGNode):
            other = self._bmg.add_constant(other)
        return self._bmg.add_not_in(input, other)

    def handle_multiplication(self, input: Any, other: Any) -> Any:
        if (not isinstance(input, BMGNode)) and (not isinstance(other, BMGNode)):
            return input * other
        if not isinstance(input, BMGNode):
            input = self._bmg.add_constant(input)
        if not isinstance(other, BMGNode):
            other = self._bmg.add_constant(other)
        if isinstance(input, ConstantNode) and isinstance(other, ConstantNode):
            return input.value * other.value
        return self._bmg.add_multiplication(input, other)

    def handle_matrix_multiplication(self, input: Any, mat2: Any) -> Any:
        if (not isinstance(input, BMGNode)) and (not isinstance(mat2, BMGNode)):
            return torch.mm(input, mat2)
        if not isinstance(input, BMGNode):
            input = self._bmg.add_constant(input)
        if not isinstance(mat2, BMGNode):
            mat2 = self._bmg.add_constant(mat2)
        if isinstance(input, ConstantNode) and isinstance(mat2, ConstantNode):
            return torch.mm(input.value, mat2.value)
        return self._bmg.add_matrix_multiplication(input, mat2)

    def handle_addition(self, input: Any, other: Any) -> Any:
        if (not isinstance(input, BMGNode)) and (not isinstance(other, BMGNode)):
            return input + other
        if not isinstance(input, BMGNode):
            input = self._bmg.add_constant(input)
        if not isinstance(other, BMGNode):
            other = self._bmg.add_constant(other)
        if isinstance(input, ConstantNode) and isinstance(other, ConstantNode):
            return input.value + other.value
        return self._bmg.add_addition(input, other)

    def handle_bitand(self, input: Any, other: Any) -> Any:
        if (not isinstance(input, BMGNode)) and (not isinstance(other, BMGNode)):
            return input & other
        if not isinstance(input, BMGNode):
            input = self._bmg.add_constant(input)
        if not isinstance(other, BMGNode):
            other = self._bmg.add_constant(other)
        return self._bmg.add_bitand(input, other)

    def handle_bitor(self, input: Any, other: Any) -> Any:
        if (not isinstance(input, BMGNode)) and (not isinstance(other, BMGNode)):
            return input | other
        if not isinstance(input, BMGNode):
            input = self._bmg.add_constant(input)
        if not isinstance(other, BMGNode):
            other = self._bmg.add_constant(other)
        return self._bmg.add_bitor(input, other)

    def handle_bitxor(self, input: Any, other: Any) -> Any:
        if (not isinstance(input, BMGNode)) and (not isinstance(other, BMGNode)):
            return input ^ other
        if not isinstance(input, BMGNode):
            input = self._bmg.add_constant(input)
        if not isinstance(other, BMGNode):
            other = self._bmg.add_constant(other)
        return self._bmg.add_bitxor(input, other)

    def handle_subtraction(self, input: Any, other: Any) -> Any:
        if (not isinstance(input, BMGNode)) and (not isinstance(other, BMGNode)):
            return input - other
        if not isinstance(input, BMGNode):
            input = self._bmg.add_constant(input)
        if not isinstance(other, BMGNode):
            other = self._bmg.add_constant(other)
        if isinstance(input, ConstantNode) and isinstance(other, ConstantNode):
            return input.value - other.value
        return self._bmg.add_subtraction(input, other)

    def handle_division(self, input: Any, other: Any) -> Any:
        if (not isinstance(input, BMGNode)) and (not isinstance(other, BMGNode)):
            return input / other
        if not isinstance(input, BMGNode):
            input = self._bmg.add_constant(input)
        if not isinstance(other, BMGNode):
            other = self._bmg.add_constant(other)
        if isinstance(input, ConstantNode) and isinstance(other, ConstantNode):
            return input.value / other.value
        return self._bmg.add_division(input, other)

    def handle_floordiv(self, input: Any, other: Any) -> Any:
        if (not isinstance(input, BMGNode)) and (not isinstance(other, BMGNode)):
            return input // other
        if not isinstance(input, BMGNode):
            input = self._bmg.add_constant(input)
        if not isinstance(other, BMGNode):
            other = self._bmg.add_constant(other)
        return self._bmg.add_floordiv(input, other)

    def handle_lshift(self, input: Any, other: Any) -> Any:
        if (not isinstance(input, BMGNode)) and (not isinstance(other, BMGNode)):
            return input << other
        if not isinstance(input, BMGNode):
            input = self._bmg.add_constant(input)
        if not isinstance(other, BMGNode):
            other = self._bmg.add_constant(other)
        return self._bmg.add_lshift(input, other)

    def handle_mod(self, input: Any, other: Any) -> Any:
        if (not isinstance(input, BMGNode)) and (not isinstance(other, BMGNode)):
            return input % other
        if not isinstance(input, BMGNode):
            input = self._bmg.add_constant(input)
        if not isinstance(other, BMGNode):
            other = self._bmg.add_constant(other)
        return self._bmg.add_mod(input, other)

    def handle_power(self, input: Any, exponent: Any) -> Any:
        if (not isinstance(input, BMGNode)) and (not isinstance(exponent, BMGNode)):
            return input ** exponent
        if not isinstance(input, BMGNode):
            input = self._bmg.add_constant(input)
        if not isinstance(exponent, BMGNode):
            exponent = self._bmg.add_constant(exponent)
        if isinstance(input, ConstantNode) and isinstance(exponent, ConstantNode):
            return input.value ** exponent.value
        return self._bmg.add_power(input, exponent)

    def handle_rshift(self, input: Any, other: Any) -> Any:
        if (not isinstance(input, BMGNode)) and (not isinstance(other, BMGNode)):
            return input >> other
        if not isinstance(input, BMGNode):
            input = self._bmg.add_constant(input)
        if not isinstance(other, BMGNode):
            other = self._bmg.add_constant(other)
        return self._bmg.add_rshift(input, other)

    def handle_index(self, left: Any, right: Any) -> Any:
        if (not isinstance(left, BMGNode)) and (not isinstance(right, BMGNode)):
            return left[right]
        if not isinstance(left, BMGNode):
            left = self._bmg.add_constant(left)
        if not isinstance(right, BMGNode):
            right = self._bmg.add_constant(right)
        return self._bmg.add_index(left, right)

    def handle_invert(self, input: Any) -> Any:
        if not isinstance(input, BMGNode):
            return ~input
        return self._bmg.add_invert(input)

    def handle_item(self, input: Any) -> Any:
        if not isinstance(input, BMGNode):
            input = self._bmg.add_constant(input)
        return self._bmg.add_item(input)

    def handle_negate(self, input: Any) -> Any:
        if not isinstance(input, BMGNode):
            return -input
        if isinstance(input, ConstantNode):
            return -input.value
        return self._bmg.add_negate(input)

    def handle_uadd(self, input: Any) -> Any:
        # Unary plus on a graph node is an identity.
        if not isinstance(input, BMGNode):
            return +input
        return input

    # TODO: Remove this. We should insert TO_REAL nodes when necessary
    # to ensure compatibility with the BMG type system.
    def handle_to_real(self, operand: Any) -> Any:
        if not isinstance(operand, BMGNode):
            return float(operand)
        if isinstance(operand, ConstantNode):
            return float(operand.value)
        return self._bmg.add_to_real(operand)

    def handle_to_int(self, operand: Any) -> Any:
        if isinstance(operand, torch.Tensor):
            return operand.int()
        if isinstance(operand, bn.ConstantTensorNode):
            return operand.value.int()
        if isinstance(operand, ConstantNode):
            return int(operand.value)
        return self._bmg.add_to_int(operand)

    def handle_exp(self, input: Any) -> Any:
        if isinstance(input, torch.Tensor):
            return torch.exp(input)
        if isinstance(input, bn.ConstantTensorNode):
            return torch.exp(input.value)
        if not isinstance(input, BMGNode):
            return math.exp(input)
        if isinstance(input, ConstantNode):
            return math.exp(input.value)
        return self._bmg.add_exp(input)

    def handle_expm1(self, input: Any) -> Any:
        if isinstance(input, torch.Tensor):
            return torch.expm1(input)
        if isinstance(input, bn.ConstantTensorNode):
            return torch.expm1(input.value)
        if not isinstance(input, BMGNode):
            return torch.expm1(input)
        if isinstance(input, ConstantNode):
            return torch.expm1(input.value)
        return self._bmg.add_expm1(input)

    def handle_logistic(self, input: Any) -> Any:
        if isinstance(input, torch.Tensor):
            return torch.sigmoid(input)
        if isinstance(input, bn.ConstantTensorNode):
            return torch.sigmoid(input.value)
        if not isinstance(input, BMGNode):
            return torch.sigmoid(input)
        if isinstance(input, ConstantNode):
            return torch.sigmoid(input.value)
        return self._bmg.add_logistic(input)

    def handle_not(self, input: Any) -> Any:
        if not isinstance(input, BMGNode):
            return not input
        if isinstance(input, ConstantNode):
            return not input.value
        return self._bmg.add_not(input)

    def handle_phi(self, input: Any) -> Any:
        if not isinstance(input, BMGNode):
            return phi(input)
        if isinstance(input, ConstantNode):
            return phi(input.value)
        return self._bmg.add_phi(input)

    def handle_log(self, input: Any) -> Any:
        if isinstance(input, torch.Tensor):
            return torch.log(input)
        if isinstance(input, bn.ConstantTensorNode):
            return torch.log(input.value)
        if not isinstance(input, BMGNode):
            return math.log(input)
        if isinstance(input, ConstantNode):
            return math.log(input.value)
        return self._bmg.add_log(input)

    def handle_log1mexp(self, input: Any) -> Any:
        if isinstance(input, torch.Tensor):
            return log1mexp(input)
        if isinstance(input, bn.ConstantTensorNode):
            return log1mexp(input.value)
        if not isinstance(input, BMGNode):
            return math_log1mexp(input)
        if isinstance(input, ConstantNode):
            return math_log1mexp(input.value)
        return self._bmg.add_log1mexp(input)

    def handle_logsumexp(self, input: Any, dim: Any, keepdim: Any = False) -> Any:
        if (
            not isinstance(input, BMGNode)
            and not isinstance(dim, BMGNode)
            and not isinstance(keepdim, BMGNode)
        ):
            # None of them are graph nodes. Just return the tensor.
            return torch.logsumexp(input=input, dim=dim, keepdim=keepdim)
        # One of them is a graph node. Make them all graph nodes.
        if not isinstance(input, BMGNode):
            input = self._bmg.add_constant(input)
        if not isinstance(dim, BMGNode):
            dim = self._bmg.add_constant(dim)
        if not isinstance(keepdim, BMGNode):
            keepdim = self._bmg.add_constant(keepdim)
        return self._bmg.add_logsumexp_torch(input, dim, keepdim)

    def _canonicalize_function(
        self, function: Any, arguments: List[Any], kwargs: Optional[Dict[str, Any]]
    ):
        """The purpose of this helper method is to uniformly handle all
        possibility for function calls. That is, the receiver can be stochastic,
        normal, or missing, and the arguments can be stochastic or normal values,
        and can be positional or named. We take in an object representing the function
        (possibly a KnownFunction helper that is bound to a receiver), and the arguments.
        We produce a function object that requires no receiver, and an argument list
        that has the receiver, if any, as its first member."""
        if kwargs is None:
            kwargs = {}
        if isinstance(function, KnownFunction):
            f = function.function
            args = [function.receiver] + arguments
            assert isinstance(f, Callable)
        elif (
            isinstance(function, builtin_function_or_method)
            and isinstance(function.__self__, torch.Tensor)
            and function.__name__ in known_tensor_instance_functions
        ):
            f = getattr(torch.Tensor, function.__name__)
            args = [function.__self__] + arguments
            assert isinstance(f, Callable)
        elif isinstance(function, MethodType):
            # In Python, if we are calling a method of a class with a "self"
            # parameter then the callable we get is already partially evaluated.
            # That is, if we have
            #
            # class C:
            #   def m(self, x):...
            # c = C();
            # cm = c.m
            # cm(1)
            #
            # Then cm is a brand-new object with attributes __self__ and __func__,
            # and calling cm(1) actually calls cm.__func__(cm.__self__, 1)
            #
            # We simulate that here.
            f = function.__func__
            args = [function.__self__] + arguments
            assert isinstance(f, Callable)

        elif isinstance(function, Callable):
            f = function
            args = arguments
        else:
            raise ValueError(
                f"Function {function} is not supported by Bean Machine Graph."
            )
        return (f, args, kwargs)

    def _create_optimized_map(
        self, choice: BMGNode, key_value_pairs: List[BMGNode]
    ) -> BMGNode:
        assert len(key_value_pairs) % 2 == 0

        num_pairs = len(key_value_pairs) / 2

        # It should be impossible to have a distribution with no support.
        assert num_pairs != 0

        # It is bizarre to have a distribution with support of one but
        # I suppose it could happen if we had something like a categorical
        # with only one category, or we had a Boolean multiplied by zero.
        # In this case we can simply use the value.

        if num_pairs == 1:
            return key_value_pairs[1]

        # If we have a map where the keys are 0 and 1 then we can
        # create an if-then-else. (It is easier to simply do it here
        # where the map is created, than to add the map and then transform
        # it in a problem fixing pass later. No reason to not do it right
        # the first time.)

        # TODO: This optimization is only valid if the choice is of type
        # boolean, but we want to decouple the runtime from the type system.
        # Move the optimization out of here and into a rewriting pass.

        if num_pairs == 2:
            if bn.is_zero(key_value_pairs[0]) and bn.is_one(key_value_pairs[2]):
                return self._bmg.add_if_then_else(
                    choice, key_value_pairs[3], key_value_pairs[1]
                )
            if bn.is_one(key_value_pairs[0]) and bn.is_zero(key_value_pairs[2]):
                return self._bmg.add_if_then_else(
                    choice, key_value_pairs[1], key_value_pairs[3]
                )

        # Otherwise, just make a map node.
        # TODO: Fix this. We need a better abstraction that is supported by BMG.
        map_node = self._bmg.add_map(*key_value_pairs)
        index_node = self._bmg.add_index_deprecated(map_node, choice)
        return index_node

    def _handle_random_variable_call_checked(
        self, function: Any, arguments: List[Any]
    ) -> BMGNode:
        assert isinstance(arguments, list)

        # Identify the index of the leftmost graph node argument:

        index = next(
            (i for i, arg in enumerate(arguments) if isinstance(arg, BMGNode)), -1
        )
        if index == -1:
            # There were no graph node arguments. Just make an ordinary
            # function call
            rv = function(*arguments)
            assert isinstance(rv, RVIdentifier)
            return self._rv_to_node(rv)

        # We have an RV call where one or more arguments are graph nodes;
        # each graph node has finite support and the estimate of the number
        # of combinations we have to try is small.

        # Replace the given argument with all possible values and recurse.
        #
        # TODO: Note that we only memoize calls to RVs when the arguments
        # contain no graph nodes. Is this acceptable? We could save some
        # work if we also memoized calls of the form "rv1(rv2())". Right now
        # we would recompute the support of rv2() on the second such call,
        # and only get the savings of skipping the method calls on each
        # individual call.  Do some performance testing.

        replaced_arg = arguments[index]
        key_value_pairs = []
        for new_arg in replaced_arg.support():
            key = self._bmg.add_constant(new_arg)
            new_arguments = list(arguments)
            new_arguments[index] = new_arg
            value = self._handle_random_variable_call_checked(function, new_arguments)
            key_value_pairs.append(key)
            key_value_pairs.append(value)
        return self._create_optimized_map(replaced_arg, key_value_pairs)

    def _handle_random_variable_call(
        self, function: Any, arguments: List[Any], kwargs: Dict[str, Any]
    ) -> BMGNode:

        if len(kwargs) != 0:
            # TODO: Better error
            raise ValueError(
                "Random variable function calls must not have named arguments."
            )

        # If we have one or more graph nodes as arguments to an RV function call
        # then we need to try every possible value for those arguments. We require
        # that there be a finite number of possibilities, and that the total number
        # of branches generated for this call is small. Check that *once* before
        # recursively processing the call one argument at a time.

        # TODO: Make this a global tweakable setting of the accumulator.
        max_possibilities = 1000
        possibilities = 1
        for arg in arguments:
            if isinstance(arg, BMGNode):
                possibilities *= arg.support_size()
                if possibilities == bn.positive_infinity:
                    # TODO: Better exception
                    raise ValueError(
                        "Stochastic control flow must have finite support."
                    )
                if possibilities > max_possibilities:
                    # TODO: Better exception
                    raise ValueError("Stochastic control flow is too complex.")

        return self._handle_random_variable_call_checked(function, arguments)

    def _handle_functional_call(
        self, function: Any, arguments: List[Any], kwargs: Dict[str, Any]
    ) -> BMGNode:

        if len(kwargs) != 0:
            # TODO: Better error
            raise ValueError("Functional calls must not have named arguments.")

        # TODO: What happens if we have a @functional that does not return
        # TODO: a graph node? A functional that returns a constant is
        # TODO: weird, but it should be legal.  Figure out what the
        # TODO: right thing to do is for this scenario.

        # We have a call to a functional function. There are two
        # cases. Either we have only ordinary values for arguments, or
        # we have one or more graph nodes.  *Do we need to handle these
        # two cases differently?*
        #
        # If the arguments are just plain arguments then we can call the
        # function normally, obtain an RVID back, and then use our usual
        # mechanism for turning an RVID into a graph node.
        #
        # What if the arguments are graph nodes? We can just do the same!
        # The callee will immediately return an RVID capturing the values
        # of the graph nodes. We then check to see if this exact call
        # has happened already; if it has, then we use the cached graph
        # node from our RVID->node cache. If it has not, we call the lifted
        # version of the method with the graph node arguments taken from
        # the RVID, and add the resulting graph node to the cache.
        #
        # Since this is a functional, not a random_variable, there is no
        # stochastic control flow to handle; we just pass the graph nodes in
        # as values and let the lifted method handle them.
        #
        # We lose nothing by doing this and we gain memoization that allows
        # us to skip doing the call if we have done it before. That's a win.

        rv = function(*arguments)
        assert isinstance(rv, RVIdentifier)
        return self._rv_to_node(rv)

    def _handle_ordinary_call(
        self, function: Callable, arguments: List[Any], kwargs: Dict[str, Any]
    ) -> Any:
        if not isinstance(function, Callable):
            raise TypeError(
                f"_handle_ordinary_call requires Callable but got {type(function)}"
            )
        # We have an ordinary function call to a function that is not on
        # our list of special functions, and is not a functional, and
        # is not a random variable.  We still need to lift the function
        # even if its arguments are not graph nodes though! It might do
        # arithmetic on a random variable even though it is not a functional.
        # For example, we might have something like:
        #
        # @random_variable
        # def norm1():
        #   return Normal(0, 1)
        #
        # # not a functional
        # def add_one():        # We call this function with no arguments
        #   return norm1() + 1
        #
        # @random_variable
        # def norm2():
        #   return Normal(add_one(), 1)
        #
        # Ideally we would like add_one to be marked as functional, but
        # given that it is not, we need to detect the call to add_one()
        # as returning a graph node that represents the sum of a sample
        # and a constant.

        # It is not already compiled; if we have source code, compile it.
        if _has_source_code(function):
            return self._function_to_bmg_function(function)(*arguments, **kwargs)
        # It is not compiled and we have no source code to compile.
        # Just call it and hope for the best.
        # TODO: Do we need to consider the scenario where we do not have
        # source code, we call a function, and it somehow returns an RVID?
        # We *could* convert that to a graph node.
        return function(*arguments, **kwargs)

    def _handle_tensor_constructor(self, data: Any, kwargs: Dict[str, Any]) -> Any:
        # TODO: Handle kwargs

        # The tensor constructor is a bit tricky because it takes a single
        # argument that is either a value or a list of values.  We need:
        # (1) a flattened list of all the arguments, and
        # (2) the size of the original tensor.

        flattened_args = list(_flatten_all_lists(data))
        if not any(isinstance(arg, BMGNode) for arg in flattened_args):
            # None of the arguments are graph nodes. We can just
            # construct the tensor normally.
            return torch.tensor(data, **kwargs)
        # At least one of the arguments is a graph node.
        #
        # If we're constructing a singleton tensor and the single value
        # is a graph node, we can just keep it as that graph node.
        if len(flattened_args) == 1:
            return flattened_args[0]

        # We have two or more arguments and at least one is a graph node.
        # Convert them all to graph nodes.
        for index, arg in enumerate(flattened_args):
            if not isinstance(arg, BMGNode):
                flattened_args[index] = self._bmg.add_constant(arg)

        # What shape is this tensor? Rather than duplicating the logic in the
        # tensor class, let's just construct the same shape made of entirely
        # zeros and then ask what shape it is.
        size = torch.tensor(_list_to_zeros(data)).size()
        return self._bmg.add_tensor(size, *flattened_args)

    def handle_function(
        self,
        function: Any,
        arguments: List[Any],
        kwargs: Optional[Dict[str, Any]] = None,
    ) -> Any:
        f, args, kwargs = self._canonicalize_function(function, arguments, kwargs)
        assert isinstance(f, Callable), (
            "_canonicalize_function should return callable "
            + f"but got {type(f)} {str(f)}"
        )

        if _is_phi(f, args, kwargs):
            return self.handle_phi(*(args[1:]), **kwargs)

        if _is_random_variable_call(f):
            return self._handle_random_variable_call(f, args, kwargs)

        if _is_functional_call(f):
            return self._handle_functional_call(f, args, kwargs)

        # If we get here, we have a function call from a module that
        # is not already compiled, and it is not a random variable
        # or functional.

        # We have special processing if we're trying to create a tensor;
        # if any element of the new tensor is a graph node then we'll
        # need to create a TensorNode.

        if f is torch.tensor:
            if len(args) != 1:
                raise TypeError(
                    "tensor() takes 1 positional argument but"
                    + f" {len(args)} were given"
                )
            return self._handle_tensor_constructor(args[0], kwargs)

        if _hashable(f):
            # Some functions are perfectly safe for a graph node.
            # We do not need to compile them.
            if f in allowed_functions:
                return f(*args, **kwargs)

            # Some functions we already have special-purpose handlers for,
            # like calls to math.exp or tensor.log. If we know there are no
            # graph nodes in the arguments we can just call the function directly
            # and get the values. If there are graph nodes in the arguments then
            # we can call our special handlers.

            # TODO: Do a sanity check that the arguments match and give
            # TODO: a good error if they do not. Alternatively, catch
            # TODO: the exception if the call fails and replace it with
            # TODO: a more informative error.
            if f in self.function_map:
                if only_ordinary_arguments(args, kwargs):
                    return f(*args, **kwargs)
                return self.function_map[f](*args, **kwargs)

        return self._handle_ordinary_call(f, args, kwargs)

    def _function_to_bmg_function(self, function: Callable) -> Callable:
        from beanmachine.ppl.compiler.bm_to_bmg import _bm_function_to_bmg_function

        # TODO: What happens if the function is a member of a class?
        # TODO: Do we recompile it for different instances of the
        # TODO: receiver? Do we recompile it on every call? Check this.
        if function not in self.lifted_map:
            self.lifted_map[function] = _bm_function_to_bmg_function(function, self)
        return self.lifted_map[function]

    def _rv_to_node(self, rv: RVIdentifier) -> BMGNode:
        key = MemoizationKey(rv.wrapper, rv.arguments)
        if key not in self.rv_map:
            if key in self.in_flight:
                # TODO: Better error message
                raise RecursionError()
            self.in_flight.add(key)
            try:
                value = self._function_to_bmg_function(rv.function)(*rv.arguments)
                # If we are calling a random_variable then we must have gotten
                # back a distribution. This is the first time we have called this
                # rv with these arguments -- because we had a cache miss -- and
                # therefore we should generate a new sample node.  If by contrast
                # we are calling a functional then we check below that we got
                # back either a graph node or a tensor that we can make into a constant.
                if rv.is_random_variable:
                    value = self.handle_sample(value)
            finally:
                self.in_flight.remove(key)
            if isinstance(value, torch.Tensor):
                value = self._bmg.add_constant_tensor(value)
            if not isinstance(value, BMGNode):
                raise TypeError("A functional must return a tensor.")
            self.rv_map[key] = value
            return value
        return self.rv_map[key]

    def collection_to_map(self, collection) -> bn.MapNode:
        if isinstance(collection, bn.MapNode):
            return collection
        if isinstance(collection, list):
            copy = []
            for i in range(0, len(collection)):
                copy.append(self._bmg.add_constant(i))
                item = collection[i]
                node = (
                    item if isinstance(item, BMGNode) else self._bmg.add_constant(item)
                )
                copy.append(node)
            return self._bmg.add_map(*copy)
        # TODO: Dictionaries? Tuples?
        raise ValueError("collection_to_map requires a list")

    def handle_sample(self, operand: Any) -> bn.SampleNode:  # noqa
        """As we execute the lifted program, this method is called every
        time a model function decorated with @bm.random_variable returns; we verify that the
        returned value is a distribution that we know how to accumulate into the
        graph, and add a sample node to the graph."""

        if isinstance(operand, bn.DistributionNode):
            return self._bmg.add_sample(operand)
        if not isinstance(operand, torch.distributions.Distribution):
            # TODO: Better error
            raise TypeError("A random_variable is required to return a distribution.")
        if isinstance(operand, dist.Bernoulli):
            b = self.handle_bernoulli(operand.probs)
            return self._bmg.add_sample(b)
        if isinstance(operand, dist.Binomial):
            b = self.handle_binomial(operand.total_count, operand.probs)
            return self._bmg.add_sample(b)
        if isinstance(operand, dist.Categorical):
            b = self.handle_categorical(operand.probs)
            return self._bmg.add_sample(b)
        if isinstance(operand, dist.Dirichlet):
            b = self.handle_dirichlet(operand.concentration)
            return self._bmg.add_sample(b)
        if isinstance(operand, dist.Chi2):
            b = self.handle_chi2(operand.df)
            return self._bmg.add_sample(b)
        if isinstance(operand, dist.Gamma):
            b = self.handle_gamma(operand.concentration, operand.rate)
            return self._bmg.add_sample(b)
        if isinstance(operand, dist.HalfCauchy):
            b = self.handle_halfcauchy(operand.scale)
            return self._bmg.add_sample(b)
        if isinstance(operand, dist.Normal):
            b = self.handle_normal(operand.mean, operand.stddev)
            return self._bmg.add_sample(b)
        if isinstance(operand, dist.HalfNormal):
            b = self.handle_halfnormal(operand.scale)
            return self._bmg.add_sample(b)
        if isinstance(operand, dist.StudentT):
            b = self.handle_studentt(operand.df, operand.loc, operand.scale)
            return self._bmg.add_sample(b)
        if isinstance(operand, dist.Uniform):
            b = self.handle_uniform(operand.low, operand.high)
            return self._bmg.add_sample(b)
        # TODO: Get this into alpha order
        if isinstance(operand, dist.Beta):
            b = self.handle_beta(operand.concentration1, operand.concentration0)
            return self._bmg.add_sample(b)
        if isinstance(operand, dist.Poisson):
            b = self.handle_poisson(operand.rate)
            return self._bmg.add_sample(b)
        # TODO: Better error
        n = type(operand).__name__
        raise TypeError(f"Distribution '{n}' is not supported by Bean Machine Graph.")

    def handle_dot_get(self, operand: Any, name: str) -> Any:
        # If we have x = foo.bar, foo must not be a sample; we have no way of
        # representing the "get the value of an attribute" operation in BMG.
        # However, suppose foo is a distribution of tensors; we do wish to support
        # operations such as:
        # x = foo.exp
        # y = x()
        # and have y be a graph that applies an EXP node to the SAMPLE node for foo.
        # This will require some cooperation between handling dots and handling
        # functions.

        # TODO: There are a great many more pure instance functions on tensors;
        # TODO: which do we wish to support?

        if isinstance(operand, BMGNode):
            # If we're invoking a function on a graph node during execution of
            # the lifted program, that graph node is almost certainly a tensor
            # in the original program; assume that it is, and see if this is
            # a function on a tensor that we know how to accumulate into the graph.
            if name in known_tensor_instance_functions:
                return KnownFunction(operand, getattr(torch.Tensor, name))
            raise ValueError(
                f"Fetching the value of attribute {name} is not "
                + "supported in Bean Machine Graph."
            )
        return getattr(operand, name)

    def handle_dot_set(self, operand: Any, name: str, value: Any) -> None:
        # If we have foo.bar = x, foo must not be a sample; we have no way of
        # representing the "set the value of an attribute" operation in BMG.
        if isinstance(operand, BMGNode):
            raise ValueError(
                f"Setting the value of attribute {name} is not "
                + "supported in Bean Machine Graph."
            )
        setattr(operand, name, value)

    def accumulate_graph(
        self,
        queries: List[RVIdentifier],
        observations: Dict[RVIdentifier, Any],
    ) -> BMGraphBuilder:
        _verify_queries_and_observations(queries, observations, True)
        self._bmg._pd = self._pd
        self._begin(prof.accumulate)
        for rv, val in observations.items():
            node = self._rv_to_node(rv)
            assert isinstance(node, bn.SampleNode)
            self._bmg.add_observation(node, val)
        for qrv in queries:
            node = self._rv_to_node(qrv)
            q = self._bmg.add_query(node)
            self._rv_to_query[qrv] = q
        self._finish(prof.accumulate)
        return self._bmg
