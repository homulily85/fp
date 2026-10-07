import tempfile
import unittest
from pathlib import Path

from pysat.solvers import Solver

from ffp_sat.instance import Instance
from ffp_sat.separator_benders import _build_master, _decode_master
from ffp_sat.stratified_benders import _master_assumptions, run_stratified_separator_benders


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


def path_instance(n):
    adjacency = [set() for _ in range(n)]
    for vertex in range(n - 1):
        adjacency[vertex].add(vertex + 1)
        adjacency[vertex + 1].add(vertex)
    return Instance(tuple(map(frozenset, adjacency)), frozenset({0}))


class StratifiedSeparatorBendersTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_safe_threshold_assumptions_can_descend_after_cut(self):
        instance = path_instance(5)
        with Solver(name="cadical300") as solver:
            formula = _build_master(instance, 1, solver)
            try:
                q3 = _master_assumptions(formula, 3)
                self.assertTrue(solver.solve(assumptions=q3))
                separator, _raw_safe, safe = _decode_master(solver, formula, instance, 1)
                self.assertEqual(separator, [1])
                self.assertEqual(len(safe), 3)

                # A globally valid no-good for the sole q=3 separator closes
                # that stratum; the same persistent master remains SAT at q=2.
                solver.add_clause([-formula["r"][1]])
                self.assertFalse(solver.solve(assumptions=_master_assumptions(formula, 3)))
                self.assertTrue(solver.solve(assumptions=_master_assumptions(formula, 2)))
                separator, _raw_safe, safe = _decode_master(solver, formula, instance, 1)
                self.assertEqual(separator, [2])
                self.assertEqual(len(safe), 2)
            finally:
                formula["r_at_most"].close()

    def test_end_to_end_phase_a_witness_finds_dynamic_solution(self):
        instance = path_instance(5)
        path = self.root / "path.in"
        write_instance(path, instance)
        report = run_stratified_separator_benders(
            path,
            firefighters=1,
            horizon=1,
            burned_bound=2,
            solver_name="cadical300",
            max_iterations_per_level=8,
            subproblem_time=5,
            subproblem_retry_time=5,
            core_minimize_soft_limit=1,
            static_search_soft_limit=5,
            total_time=30,
            run_equivalence_regression=False,
        )
        self.assertEqual(report["static_search"]["q_max"], 3)
        self.assertTrue(report["static_search"]["proved"])
        self.assertEqual(report["result"], "FOUND_DYNAMIC_SOLUTION")
        self.assertEqual(report["solution"]["level_q"], 3)
        self.assertTrue(report["solution"]["validated"])
        self.assertLessEqual(report["solution"]["actual_k"], 1)


if __name__ == "__main__":
    unittest.main()
