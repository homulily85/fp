import tempfile
import unittest
from pathlib import Path

from pysat.solvers import Solver

from ffp_sat.encoder import Encoder
from ffp_sat.prefix_core_mining import guarded_core_clause
from ffp_sat.prefix_trie_mining import (
    build_prefix_trie,
    ordered_children,
    run_trie_probe,
    traverse_prefix_trie,
)
from ffp_sat.preprocess import preprocess
from tests.helpers import graph


def write_instance(path, instance):
    edges = [
        (u, v)
        for u, neighbors in enumerate(instance.adjacency)
        for v in neighbors
        if u < v
    ]
    rows = ["0", str(instance.n), str(len(edges)), "test", str(len(instance.initial_fire))]
    rows.append(" ".join(map(str, sorted(instance.initial_fire))))
    rows.extend(f"{u} {v}" for u, v in edges)
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")


class PrefixTrieMiningTests(unittest.TestCase):
    def test_cadical_propagate_reports_assumption_contradiction(self):
        with Solver(name="cadical300", bootstrap_with=[[-1, -2]]) as solver:
            consistent, _ = solver.propagate(assumptions=[1, 2])
        self.assertFalse(consistent)

    def test_trie_counts_coverage_and_orders_high_coverage_first(self):
        root = build_prefix_trie(
            [
                {"prefix": [[1, 568], [2, 10]], "source_k": 10},
                {"prefix": [[1, 568], [2, 11]], "source_k": 12},
                {"prefix": [[1, 659], [2, 12]], "source_k": 9},
            ],
            2,
        )
        ordered = ordered_children(root)
        self.assertEqual([vertex for vertex, _ in ordered], [568, 659])
        self.assertEqual(root.children[568].source_count, 2)
        self.assertEqual(root.children[568].children[10].source_count, 1)
        self.assertEqual(root.children[568].best_source_k, 10)

    def test_timeout_parent_is_expanded_and_unsat_child_is_learned(self):
        root = build_prefix_trie(
            [
                {"prefix": [[1, 1], [2, 2], [3, 3]], "source_k": 5},
                {"prefix": [[1, 1], [2, 4], [3, 5]], "source_k": 6},
            ],
            3,
        )
        calls = []

        def probe(prefix, budget, _node):
            calls.append((prefix, budget))
            if len(prefix) == 1:
                return {"result": "TIMEOUT"}
            return {"result": "UNSAT_PROPAGATION"}

        result = traverse_prefix_trie(root, probe, [60, 20, 10])
        self.assertEqual(len(calls), 3)
        self.assertEqual([row["depth"] for row in result["probes"]], [1, 2, 2])
        self.assertEqual(len(result["learned_prefixes"]), 2)
        self.assertEqual(result["pruned_schedules"], 2)

    def test_unsat_parent_prunes_all_descendants(self):
        root = build_prefix_trie(
            [{"prefix": [[1, 1], [2, 2]], "source_k": 3}], 2
        )
        calls = []

        def probe(prefix, _budget, _node):
            calls.append(prefix)
            return {"result": "UNSAT_PROPAGATION"}

        result = traverse_prefix_trie(root, probe, [5, 1])
        self.assertEqual(calls, [((1, 1),)])
        self.assertEqual(result["pruned_descendant_nodes"], 1)
        self.assertEqual(result["learned_prefixes"][0]["depth"], 1)

    def test_fresh_probe_and_guarded_clause_preserve_base_query(self):
        instance = graph(3, [(0, 1), (1, 2)])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "path.in"
            write_instance(path, instance)
            probe = run_trie_probe(
                path,
                firefighters=1,
                solver_name="cadical300",
                horizon=2,
                bound=1,
                prefix=((1, 0),),
                timeout=5,
            )
        self.assertEqual(probe["result"], "UNSAT_PROPAGATION")

        distance, _ = preprocess(instance, 1)
        with Solver(name="cadical300") as solver:
            encoder = Encoder(instance, 1, solver, distance)
            try:
                encoder.ensure_horizon(2)
                assumptions = encoder.assumptions(2, 1, instance.n)
                self.assertTrue(solver.solve(assumptions=assumptions))
                clause = guarded_core_clause(assumptions, [(1, 0, True)], encoder)
                solver.add_clause(clause)
                self.assertTrue(solver.solve(assumptions=assumptions))
            finally:
                encoder.close()


if __name__ == "__main__":
    unittest.main()
