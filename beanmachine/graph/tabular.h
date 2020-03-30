// Copyright (c) Facebook, Inc. and its affiliates.
#pragma once
#include "beanmachine/graph/distribution.h"

namespace beanmachine {
namespace distribution {

class Tabular : public Distribution {
 public:
  Tabular(
      graph::AtomicType sample_type,
      const std::vector<graph::Node*>& in_nodes);
  ~Tabular() override {}
  graph::AtomicValue sample(std::mt19937& gen) const override;
  double log_prob(const graph::AtomicValue& value) const override;
  void gradient_log_prob_value(
    const graph::AtomicValue& value, double& grad1, double& grad2) const override;
  void gradient_log_prob_param(
    const graph::AtomicValue& value, double& grad1, double& grad2) const override;

 private:
  double get_probability() const;
};

} // namespace distribution
} // namespace beanmachine
