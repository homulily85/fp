import tempfile
import unittest
from pathlib import Path

from pysat.solvers import Solver

from ffp_sat.early_exact import (
    append_early_exact_actions,
    run_containment_query,
    run_small_graph_equivalence_regression,
)
from ffp_sat.encoder import Encoder
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


class EarlyExactTests(unittest.TestCase):
    def setUp(self):
        self.instance = graph(5, [(0, 1), (1, 2), (2, 3), (3, 4)])

    def test_append_is_zero_variable_exact_clause_and_idempotent(self):
        distance, _ = preprocess(self.instance, 1)
        with Solver(name="cadical300") as solver:
            encoder = Encoder(self.instance, 1, solver, distance)
            try:
                encoder.ensure_horizon(4)
                top = encoder.vars.top
                clauses = encoder.clauses
                first = append_early_exact_actions(encoder, solver, 3, "through")
                self.assertEqual(first["rounds"], [1, 2, 3])
                self.assertEqual(first["variables_added"], 0)
                self.assertEqual(first["clauses_added"], 3)
                self.assertEqual(encoder.vars.top, top)
                self.assertEqual(encoder.clauses, clauses + 3)
                second = append_early_exact_actions(encoder, solver, 3, "through")
                self.assertEqual(second["variables_added"], 0)
                self.assertEqual(second["clauses_added"], 0)
                self.assertEqual(second["added_rounds"], [])
                self.assertEqual(encoder.clauses, clauses + 3)
            finally:
                encoder.close()

    def test_before_range_excludes_the_minimum_containment_round(self):
        distance, _ = preprocess(self.instance, 1)
        with Solver(name="cadical300") as solver:
            encoder = Encoder(self.instance, 1, solver, distance)
            try:
                encoder.ensure_horizon(4)
                result = append_early_exact_actions(encoder, solver, 3, "before")
                self.assertEqual(result["rounds"], [1, 2])
                self.assertEqual(result["clauses_added"], 2)
            finally:
                encoder.close()

    def test_containment_only_query_has_no_objective_counter(self):
        isolated = graph(2, [], fire=(0,))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "isolated.in"
            write_instance(path, isolated)
            result = run_containment_query(path, 1, "cadical300", 0, timeout=5)
        self.assertEqual(result["result"], "SAT")
        self.assertEqual(len(result["assumptions"]), 1)
        self.assertFalse(result["objective_counters"])
        self.assertEqual(result["actual_containment_time"], 0)

    def test_small_graph_bruteforce_and_sat_equivalence_regression(self):
        report = run_small_graph_equivalence_regression()
        self.assertTrue(report["passed"])
        self.assertGreaterEqual(report["graph_cases"], 80)
        self.assertGreater(report["queries_checked"], 900)
        self.assertEqual(report["last_containment_round_case"]["T_min"], 2)


if __name__ == "__main__":
    unittest.main()
