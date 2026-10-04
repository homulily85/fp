import unittest

from pysat.solvers import Solver

from ffp_sat.encoder import Encoder
from ffp_sat.preprocess import preprocess
from ffp_sat.round1_groups import (
    _coverage_snapshot,
    assert_nested,
    assert_partition,
    modulo_partition,
)
from tests.helpers import graph


class Round1GroupTests(unittest.TestCase):
    def test_modulo_groups_are_disjoint_and_exhaustive(self):
        ranked = list(range(23))
        groups = modulo_partition(ranked, 8)
        assert_partition(groups, ranked)
        self.assertEqual(groups[0], [0, 8, 16])
        self.assertEqual(groups[7], [7, 15])

    def test_modulo_groups_refine_exactly_when_doubled(self):
        ranked = list(range(29))
        groups8 = modulo_partition(ranked, 8)
        groups16 = modulo_partition(ranked, 16)
        for group_id, parent in groups8.items():
            assert_nested(parent, [groups16[group_id], groups16[group_id + 8]])

    def test_parent_child_refinement_rejects_missing_vertices(self):
        with self.assertRaises(AssertionError):
            assert_nested([1, 2, 3], [[1], [3]])

    def test_unknown_coverage_is_not_counted_as_eliminated(self):
        report = {
            "ranking": {"ranked_vertices": list(range(8))},
            "probes": [{"level": 8, "group_id": group, "result": "UNKNOWN_BUDGET"} for group in range(8)],
        }
        eliminated, unresolved = _coverage_snapshot(report, 8)
        self.assertEqual(eliminated, [])
        self.assertEqual(unresolved, list(range(8)))

    def test_unsat_parent_eliminates_its_whole_partition(self):
        report = {
            "ranking": {"ranked_vertices": list(range(8))},
            "probes": [{"level": 8, "group_id": 0, "result": "UNSAT"}],
        }
        eliminated, unresolved = _coverage_snapshot(report, 16)
        self.assertEqual(eliminated, [0])
        self.assertEqual(unresolved, list(range(1, 8)))

    def test_selector_group_union_matches_round_one_exact_query(self):
        instance = graph(4, [(0, 1), (1, 2), (2, 3)])
        distance, _ = preprocess(instance, 1)
        ranked = [2, 0, 3, 1]
        groups = modulo_partition(ranked, 2)
        with Solver(name="cadical300") as solver:
            encoder = Encoder(instance, 1, solver, distance)
            try:
                encoder.ensure_horizon(3)
                query = encoder.assumptions(3, 4, instance.n)
                encoder.add([encoder.a[v, 1] for v in range(instance.n)])
                base = solver.solve(assumptions=query)
                selectors = {}
                group_results = []
                for group_id, vertices in groups.items():
                    selector = encoder.vars.new_aux(f"test_group[{group_id}]")
                    selectors[group_id] = selector
                    encoder.add([-selector, *[encoder.a[v, 1] for v in vertices]])
                    assumptions = (
                        query + [selector] + [-other for other in selectors.values() if other != selector]
                    )
                    result = solver.solve(assumptions=assumptions)
                    group_results.append(result)
                    if result:
                        actions = encoder.decode(solver.get_model(), 3)[0]
                        self.assertEqual(len(actions), 1)
                        self.assertIn(actions[0], vertices)
                self.assertEqual(base, any(group_results))
            finally:
                encoder.close()


if __name__ == "__main__":
    unittest.main()
