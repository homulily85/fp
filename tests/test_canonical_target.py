import unittest

from ffp_sat.canonical_target import (
    _canonical_target_query,
    _pair_canonicalization_regression,
    canonicalize_schedule_with_two_untouched,
)
from ffp_sat.instance import Instance
from ffp_sat.simulator import simulate
from tests.helpers import graph


class CanonicalTargetTests(unittest.TestCase):
    def test_small_graph_equivalence_regression(self):
        result = _pair_canonicalization_regression()
        self.assertTrue(result["passed"])
        self.assertGreater(result["queries_checked"], 0)

    def test_fresh_canonical_formula_omits_objective_and_validates_sat(self):
        instance = graph(6, [(0, 1)], fire=(0,))
        result = _canonical_target_query(instance, 3, "cadical300")
        self.assertEqual(result["result"], "SAT")
        self.assertEqual(result["objective_aux_variables"], 0)
        self.assertEqual(result["objective_clauses"], 0)
        self.assertEqual(result["canonical"]["exact_action_clauses"], 3)
        self.assertEqual(result["actual_k"], 1)
        self.assertEqual(len(result["encoded_defended"]), 3)
        self.assertGreaterEqual(len(result["untouched_witnesses"]), 2)
        self.assertTrue(result["validated"])

    def test_idle_fill_keeps_two_originally_untouched_vertices_safe(self):
        instance = Instance(
            adjacency=(frozenset({1}), frozenset({0}), frozenset(), frozenset(), frozenset(), frozenset()),
            initial_fire=frozenset({0}),
        )
        canonical = canonicalize_schedule_with_two_untouched(instance, 1, 3, [[1], [], []])
        self.assertEqual([len(actions) for actions in canonical], [1, 1, 1])
        solution = simulate(instance, 1, canonical)
        untouched = set(range(instance.n)) - set(solution.burned) - set(solution.defended)
        self.assertGreaterEqual(len(untouched), 2)
        self.assertLessEqual(solution.k, 1)


if __name__ == "__main__":
    unittest.main()
