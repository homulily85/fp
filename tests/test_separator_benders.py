import json
import tempfile
import unittest
from pathlib import Path

from pysat.solvers import Solver

from ffp_sat.instance import Instance
from ffp_sat.separator_benders import (
    _build_master,
    _coverage,
    _temporal_candidate,
    run_separator_benders_pilot,
)


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


def graph(n, edges, initial=(0,)):
    adjacency = [set() for _ in range(n)]
    for u, v in edges:
        adjacency[u].add(v)
        adjacency[v].add(u)
    return Instance(tuple(map(frozenset, adjacency)), frozenset(initial))


class SeparatorBendersTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_master_enforces_exact_separator_and_two_safe_vertices(self):
        instance = graph(6, [(0, 1), (1, 2), (2, 3)])
        with Solver(name="cadical300") as solver:
            formula = _build_master(instance, 1, solver)
            try:
                self.assertTrue(solver.solve(assumptions=formula["assumptions"]))
                model = set(solver.get_model())
                separator = {v for v, lit in enumerate(formula["r"]) if lit in model}
                safe = {v for v, lit in enumerate(formula["s"]) if lit in model}
                self.assertEqual(len(separator), 1)
                self.assertGreaterEqual(len(safe), 2)
            finally:
                formula["r_at_most"].close()

    def test_temporal_unsat_core_is_semantic_and_can_be_minimized(self):
        instance = graph(6, [(0, 1), (1, 2), (2, 3)])
        path = self.root / "path.in"
        write_instance(path, instance)
        result = _temporal_candidate(instance, [3], 1, "cadical300", 5, 1)
        self.assertEqual(result["result"], "UNSAT")
        self.assertEqual(result["assumption_count"], 1)
        self.assertEqual(result["raw_core"], [3])
        self.assertEqual(result["reduced_core"], [3])
        self.assertEqual(result["objective_variables"], 0)
        self.assertEqual(result["objective_clauses"], 0)

    def test_multi_small_core_propagation_cut_matches_temporal_unsat(self):
        instance = graph(6, [(0, 1), (1, 2), (2, 3)])
        result = _temporal_candidate(
            instance, [3], 1, "cadical300", 5, 1,
            mode="multi-small-core", subset_scan_max_size=1,
            core_restarts=2, core_restart_stop_size=2,
            calibration_full_solve=1,
        )
        self.assertEqual(result["result"], "UNSAT")
        self.assertEqual(result["propagation_scan"]["cores"], [[3]])
        self.assertEqual(result["candidate_cores"][0]["source_type"], "propagation")
        self.assertEqual(result["candidate_cores"][0]["reduced"], [3])
        self.assertTrue(result["full_temporal_solved"])

    def test_multi_small_core_adds_all_non_subsumed_cuts(self):
        instance = graph(6, [(0, 1), (1, 2), (2, 3)])
        path = self.root / "path.in"
        write_instance(path, instance)
        seed_path = self.root / "seed.json"
        seed_path.write_text(json.dumps({"separator": [3], "safe_region": [4, 5]}), encoding="utf-8")
        report = run_separator_benders_pilot(
            path, firefighters=1, horizon=1, burned_bound=3,
            solver_name="cadical300", max_iterations=4,
            master_query_time=5, subproblem_time=5, subproblem_retry_time=5,
            core_minimize_time=1, total_time=30, seed_witnesses=[seed_path],
            mode="multi-small-core", subset_scan_max_size=1,
            core_restarts=2, calibration_full_solve=1,
        )
        first = report["iterations"][0]
        self.assertEqual(first["result"], "UNSAT")
        self.assertEqual(first["cuts_generated"], 1)
        self.assertEqual(first["cuts_added_this_iteration"], 1)
        self.assertEqual(report["summary"]["active_cut_histogram"], {1: 1})
        self.assertEqual(report["result"], "FOUND_DYNAMIC_SOLUTION")

    def test_end_to_end_pilot_adds_sound_cut_then_finds_sat(self):
        instance = graph(6, [(0, 1), (1, 2), (2, 3)])
        path = self.root / "path.in"
        write_instance(path, instance)
        seed_path = self.root / "seed.json"
        seed_path.write_text(json.dumps({"separator": [3], "safe_region": [4, 5]}), encoding="utf-8")
        report = run_separator_benders_pilot(
            path,
            firefighters=1,
            horizon=1,
            burned_bound=3,
            solver_name="cadical300",
            max_iterations=4,
            master_query_time=5,
            subproblem_time=5,
            subproblem_retry_time=5,
            core_minimize_time=1,
            total_time=30,
            seed_witnesses=[seed_path],
        )
        self.assertEqual(report["iterations"][0]["result"], "UNSAT")
        self.assertEqual(report["iterations"][0]["reduced_core"], [3])
        self.assertEqual(report["result"], "FOUND_DYNAMIC_SOLUTION")
        self.assertTrue(report["solution"]["validated"])
        self.assertLessEqual(report["solution"]["actual_k"], 3)

    def test_benders_unsat_proof_after_all_static_candidates_are_cut(self):
        instance = graph(5, [(0, 1), (1, 3), (0, 2), (2, 4)])
        path = self.root / "two_branches.in"
        write_instance(path, instance)
        report = run_separator_benders_pilot(
            path,
            firefighters=1,
            horizon=2,
            burned_bound=1,
            solver_name="cadical300",
            max_iterations=8,
            master_query_time=5,
            subproblem_time=5,
            subproblem_retry_time=5,
            core_minimize_time=1,
            total_time=30,
        )
        self.assertEqual(report["result"], "UNSAT_AT_HORIZON_2")
        self.assertGreaterEqual(report["summary"]["cuts_added"], 1)
        self.assertEqual(report["summary"]["temporal_sat"], 0)

    def test_cadical_solver_remains_usable_after_incremental_queries(self):
        with Solver(name="cadical300", bootstrap_with=[[-1, -2]]) as solver:
            self.assertTrue(solver.solve(assumptions=[1]))
            self.assertFalse(solver.solve(assumptions=[1, 2]))
            self.assertTrue(solver.solve(assumptions=[-1, -2]))

    def test_coverage_fraction_matches_combination_ratio(self):
        coverage = _coverage(3, 10, 4)
        self.assertAlmostEqual(coverage["fraction"], 7 / 210)


if __name__ == "__main__":
    unittest.main()
