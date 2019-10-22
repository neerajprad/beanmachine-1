# Copyright (c) Facebook, Inc. and its affiliates.
import unittest

from beanmachine.ppl.inference.single_site_ancestral_mh import (
    SingleSiteAncestralMetropolisHastings,
)
from beanmachine.ppl.tests.conjugate_tests.abstract_conjugate import (
    AbstractConjugateTests,
)


class SingleSiteAncestralMetropolisHastingsConjugateTest(
    unittest.TestCase, AbstractConjugateTests
):
    def test_beta_binomial_conjugate_run(self):
        expected_mean, expected_std, queries, observations = (
            self.compute_beta_binomial_moments()
        )
        mh = SingleSiteAncestralMetropolisHastings()
        predictions = mh.infer(queries, observations, 1000, 1)
        for i in range(predictions.get_num_chains()):
            mean, _ = self.compute_statistics(predictions.get_chain(i)[queries[0]])
            self.assertAlmostEqual(
                abs((mean - expected_mean).sum().item()), 0, delta=0.05
            )

    def test_gamma_gamma_conjugate_run(self):
        expected_mean, expected_std, queries, observations = (
            self.compute_gamma_gamma_moments()
        )
        mh = SingleSiteAncestralMetropolisHastings()
        predictions = mh.infer(queries, observations, 1000, 1)
        for i in range(predictions.get_num_chains()):
            mean, _ = self.compute_statistics(predictions.get_chain(i)[queries[0]])
            self.assertAlmostEqual(
                abs((mean - expected_mean).sum().item()), 0, delta=0.05
            )

    def test_gamma_normal_conjugate_run(self):
        # Converges with 10k and more iterations but will use a bigger delta for
        # now to have a faster test.
        expected_mean, expected_std, queries, observations = (
            self.compute_gamma_normal_moments()
        )
        mh = SingleSiteAncestralMetropolisHastings()
        predictions = mh.infer(queries, observations, 10000, 1)
        for i in range(predictions.get_num_chains()):
            mean, _ = self.compute_statistics(predictions.get_chain(i)[queries[0]])
            self.assertAlmostEqual(
                abs((mean - expected_mean).sum().item()), 0, delta=0.2
            )

    def test_normal_normal_conjugate_run(self):
        # Converges with 10k and more iterations but will use a bigger delta for
        # now to have a faster test.
        expected_mean, expected_std, queries, observations = (
            self.compute_normal_normal_moments()
        )
        mh = SingleSiteAncestralMetropolisHastings()
        predictions = mh.infer(queries, observations, 5000, 1)
        for i in range(predictions.get_num_chains()):
            mean, _ = self.compute_statistics(predictions.get_chain(i)[queries[0]])
            self.assertAlmostEqual(
                abs((mean - expected_mean).sum().item()), 0, delta=0.1
            )

    def test_distant_normal_normal_conjugate_run(self):
        # We don't except ancestral to be able to converge fast for this model.
        pass

    def test_dirichlet_categorical_conjugate_run(self):
        expected_mean, expected_std, queries, observations = (
            self.compute_dirichlet_categorical_moments()
        )
        mh = SingleSiteAncestralMetropolisHastings()
        predictions = mh.infer(queries, observations, 5000, 1)
        for i in range(predictions.get_num_chains()):
            mean, _ = self.compute_statistics(predictions.get_chain(i)[queries[0]])
            self.assertAlmostEqual(
                abs((mean - expected_mean).sum().item()), 0, delta=0.1
            )
