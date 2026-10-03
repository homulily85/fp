import random
import tempfile
import unittest
from itertools import combinations
from pathlib import Path

from pysat.solvers import Solver

from ffp_sat.canonical import CanonicalActionEncoding
from ffp_sat.diagnose import run_canonical_case
from ffp_sat.encoder import Encoder
from ffp_sat.preprocess import preprocess
from ffp_sat.simulator import simulate
from tests.helpers import graph


def active(instance, burned, defended):
    threatened = set().union(*(instance.adjacency[v] for v in burned)) - burned - defended
    return bool(threatened)


def outcomes(instance, firefighters, canonical):
    """Enumerate contained (time, burned-count) pairs under each policy."""
    result = set()
    seen = set()
    n = instance.n

    def visit(burned, defended, t):
        state = (frozenset(burned), frozenset(defended), t)
        if state in seen:
            return
        seen.add(state)
        if not active(instance, burned, defended):
            result.add((t, len(burned)))
            return
        untouched = sorted(set(range(n)) - burned - defended)
        max_actions = min(firefighters, len(untouched))
        for count in range(max_actions + 1):
            for actions_tuple in combinations(untouched, count):
                if canonical and not actions_tuple:
                    continue
                actions = set(actions_tuple)
                next_defended = defended | actions
                spread = set().union(*(instance.adjacency[v] for v in burned)) - burned - next_defended
                next_burned = burned | spread
                still_active = active(instance, next_burned, next_defended)
                if canonical and still_active and count != min(firefighters, n):
                    continue
                if t < n + 1:
                    visit(next_burned, next_defended, t + 1)

    visit(set(instance.initial_fire), set(), 0)
    return result


def all_small_graphs(n):
    possible_edges = list(combinations(range(n), 2))
    for edge_mask in range(1 << len(possible_edges)):
        edges = [edge for index, edge in enumerate(possible_edges) if edge_mask >> index & 1]
        for fire_mask in range(1, 1 << n):
            fire = tuple(v for v in range(n) if fire_mask >> v & 1)
            yield graph(n, edges, fire)


def sat_result(instance, firefighters, horizon, bound, mode):
    distance, _ = preprocess(instance, firefighters)
    solver = Solver(name="cadical300")
    encoder = Encoder(instance, firefighters, solver, distance)
    try:
        encoder.ensure_horizon(horizon)
        assumptions = encoder.assumptions(horizon, bound, instance.n)
        base_variables = encoder.vars.top
        base_clauses = encoder.clauses
        canonical = CanonicalActionEncoding(encoder)
        canonical.apply_mode(mode, horizon, firefighters)
        return solver.solve(assumptions=assumptions), (
            base_variables,
            base_clauses,
            tuple(assumptions),
        )
    finally:
        encoder.close()
        solver.delete()


class CanonicalActionTests(unittest.TestCase):
    def test_exhaustive_all_graphs_through_four_vertices(self):
        for n in range(1, 5):
            for instance in all_small_graphs(n):
                base_by_d = {}
                canonical_by_d = {}
                for firefighters in range(1, n + 1):
                    base_by_d[firefighters] = outcomes(instance, firefighters, False)
                    canonical_by_d[firefighters] = outcomes(instance, firefighters, True)
                for firefighters in range(1, n + 1):
                    base = base_by_d[firefighters]
                    canon = canonical_by_d[firefighters]
                    for horizon in range(n + 2):
                        for bound in range(n + 1):
                            base_sat = any(t <= horizon and k <= bound for t, k in base)
                            canonical_sat = any(t <= horizon and k <= bound for t, k in canon)
                            self.assertEqual(
                                base_sat,
                                canonical_sat,
                                msg=(
                                    f"n={n}, B={sorted(instance.initial_fire)}, D={firefighters}, "
                                    f"T={horizon}, K={bound}, adjacency={instance.adjacency}"
                                ),
                            )

    def test_sat_encoding_matches_base_on_seeded_random_graphs(self):
        rng = random.Random(8417)
        for _ in range(100):
            n = rng.randint(5, 8)
            edges = [
                (u, v)
                for u, v in combinations(range(n), 2)
                if rng.random() < 0.3
            ]
            fire = [v for v in range(n) if rng.random() < 0.3]
            if not fire:
                fire = [rng.randrange(n)]
            instance = graph(n, edges, fire)
            firefighters = rng.randint(1, min(3, n))
            horizon = rng.randint(0, min(5, n))
            bound = rng.randint(0, n)
            base_sat, base_signature = sat_result(instance, firefighters, horizon, bound, "base")
            for mode in ("active-only", "stop-after-contained", "canonical"):
                mode_sat, mode_signature = sat_result(instance, firefighters, horizon, bound, mode)
                self.assertEqual(base_sat, mode_sat, msg=f"mode={mode}, sample={_}")
                self.assertEqual(base_signature, mode_signature)

    def test_active_variables_match_simulator_with_fixed_actions(self):
        cases = [
            (graph(4, [(0, 1), (1, 2), (2, 3)], (0,)), 1, [[1]]),
            (graph(4, [(0, 1), (1, 2), (2, 3), (3, 0)], (0,)), 1, [[1], [2]]),
            (graph(5, [(0, 1), (1, 2), (2, 3), (3, 4)], (0, 4)), 2, [[1, 3]]),
            (graph(2, [], (0,)), 1, []),
        ]
        for instance, firefighters, schedule in cases:
            horizon = max(2, len(schedule) + 1)
            solution = simulate(instance, firefighters, schedule)
            trace_burned = set(instance.initial_fire)
            trace_defended = set()
            expected = [active(instance, trace_burned, trace_defended)]
            for t in range(1, horizon):
                actions = set(solution.schedule[t - 1]) if t <= len(solution.schedule) else set()
                trace_defended.update(actions)
                spread = set().union(*(instance.adjacency[v] for v in trace_burned)) - trace_burned - trace_defended
                trace_burned.update(spread)
                expected.append(active(instance, trace_burned, trace_defended))

            distance, _ = preprocess(instance, firefighters)
            solver = Solver(name="cadical300")
            encoder = Encoder(instance, firefighters, solver, distance)
            try:
                encoder.ensure_horizon(horizon)
                assumptions = encoder.assumptions(horizon, instance.n, instance.n)
                canonical = CanonicalActionEncoding(encoder)
                canonical.ensure_active_states(horizon - 1)
                for t in range(1, horizon + 1):
                    chosen = set(solution.schedule[t - 1]) if t <= len(solution.schedule) else set()
                    assumptions.extend(
                        encoder.a[v, t] if v in chosen else -encoder.a[v, t]
                        for v in range(instance.n)
                    )
                assumptions.extend(
                    canonical.active[t] if value else -canonical.active[t]
                    for t, value in enumerate(expected)
                )
                self.assertTrue(solver.solve(assumptions=assumptions))
            finally:
                encoder.close()
                solver.delete()

    def test_diagnostic_modes_keep_base_formula_and_have_expected_overhead(self):
        instance = graph(4, [(0, 1), (1, 2), (2, 3)], (0,))
        signatures = []
        stats = {}
        for mode in ("base", "active-only", "stop-after-contained", "canonical"):
            distance, _ = preprocess(instance, 1)
            solver = Solver(name="cadical300")
            encoder = Encoder(instance, 1, solver, distance)
            try:
                encoder.ensure_horizon(3)
                assumptions = encoder.assumptions(3, 2, instance.n)
                signatures.append((encoder.vars.top, encoder.clauses, tuple(assumptions)))
                canonical = CanonicalActionEncoding(encoder)
                canonical.apply_mode(mode, 3, 1)
                stats[mode] = canonical.stats()
            finally:
                encoder.close()
                solver.delete()
        self.assertEqual(len(set(signatures)), 1)
        self.assertEqual(stats["base"]["active_variables"], 0)
        self.assertGreater(stats["active-only"]["active_variables"], 0)
        self.assertEqual(stats["active-only"]["stop_clauses"], 0)
        self.assertGreater(stats["stop-after-contained"]["stop_clauses"], 0)
        self.assertGreater(stats["canonical"]["nonempty_clauses"], 0)
        self.assertEqual(stats["canonical"]["full_capacity_clauses"], 0)

    def test_diagnostic_worker_runs_a_fresh_sat_ablation(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "path.in"
            path.write_text("0\n3\n2\nx\n1\n0\n0 1\n1 2\n")
            base = run_canonical_case(path, 1, "cadical300", 1, 2, "base", 5)
            canonical = run_canonical_case(path, 1, "cadical300", 1, 2, "canonical", 5)
            self.assertEqual(base["result"], "SAT")
            self.assertEqual(canonical["result"], "SAT")
            self.assertNotEqual(base["worker_pid"], canonical["worker_pid"])
            self.assertEqual(base["base_variables"], canonical["base_variables"])
            self.assertEqual(base["base_clauses"], canonical["base_clauses"])


if __name__ == "__main__":
    unittest.main()
