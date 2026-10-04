import unittest

from ffp_sat.static_dynamic_bridge import _small_graph_pair_regression, _solve_temporal_query
from tests.helpers import graph


class StaticDynamicBridgeTests(unittest.TestCase):
    def test_pair_exact_canonicalization_matches_base_on_small_graphs(self):
        report = _small_graph_pair_regression()
        self.assertTrue(report["passed"])
        self.assertGreater(report["pair_queries_checked"], 0)

    def test_pair_only_query_omits_objective_and_adds_one_action_clause(self):
        instance = graph(4, [(0, 1), (1, 2), (2, 3)], fire=(0,))
        result = _solve_temporal_query(
            instance,
            firefighters=1,
            horizon=1,
            solver_name="cadical300",
            mode="pair-only",
            pair={2, 3},
        )
        self.assertEqual(result["result"], "SAT")
        self.assertEqual(result["objective_aux_variables"], 0)
        self.assertEqual(result["objective_clauses"], 0)
        self.assertEqual(result["pair_state_unit_clauses"], 4)
        self.assertEqual(result["pair_exact_action_clauses"], 1)
        self.assertEqual(result["actual_k"], 1)
        self.assertEqual(result["containment_time"], 1)


if __name__ == "__main__":
    unittest.main()
