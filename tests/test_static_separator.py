import unittest

from ffp_sat.static_separator import (
    _static_query,
    brute_static_optimum,
    validate_static_witness,
)
from tests.helpers import graph


class StaticSeparatorTests(unittest.TestCase):
    def test_static_cnf_matches_separator_oracle(self):
        instances = [
            graph(4, [(0, 1), (1, 2), (2, 3)]),
            graph(5, [(0, 1), (0, 2), (1, 3), (2, 4)]),
            graph(5, [(0, 1), (1, 2), (2, 3), (3, 4)], fire=(0, 4)),
        ]
        for instance in instances:
            budget = min(2, instance.n)
            optimum, _ = brute_static_optimum(instance, budget)
            for target in range(instance.n + 1):
                satisfiable, witness = _static_query(instance, budget, target)
                self.assertEqual(satisfiable, optimum >= target)
                if satisfiable:
                    self.assertTrue(witness["validated"])

    def test_graph_validator_canonicalizes_disconnected_safe_vertices(self):
        instance = graph(5, [(0, 1), (1, 2), (3, 4)])
        witness = validate_static_witness(
            instance,
            defense_budget=1,
            saved_target=2,
            separator={1},
            safe_region={2},
        )
        self.assertEqual(witness["canonical_safe_region"], [2, 3, 4])
        self.assertEqual(witness["static_saved"], 4)

    def test_validator_rejects_safe_vertex_reachable_after_separator_removal(self):
        instance = graph(3, [(0, 1), (1, 2)])
        with self.assertRaises(AssertionError):
            validate_static_witness(
                instance,
                defense_budget=1,
                saved_target=1,
                separator=set(),
                safe_region={1},
            )


if __name__ == "__main__":
    unittest.main()
