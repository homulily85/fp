import random
import tempfile
import unittest
from itertools import combinations
from pathlib import Path

from pysat.solvers import Solver

from ffp_sat.action_canonical import ActionCanonicalEncoding
from ffp_sat.diagnose import run_action_canonical_case
from ffp_sat.encoder import Encoder
from ffp_sat.preprocess import preprocess
from ffp_sat.simulator import simulate
from tests.helpers import graph

MODES = ("base", "indicator-only", "prefix", "canonical")


def outcomes(instance, firefighters, mode):
    result = set()
    seen = set()
    n = instance.n

    def visit(burned, defended, t, action_counts):
        state = (frozenset(burned), frozenset(defended), t, action_counts)
        if state in seen:
            return
        seen.add(state)
        threatened = set().union(*(instance.adjacency[v] for v in burned)) - burned - defended
        if not threatened:
            result.add((t, len(burned)))
            return
        untouched = sorted(set(range(n)) - burned - defended)
        for count in range(min(firefighters, len(untouched)) + 1):
            for actions_tuple in combinations(untouched, count):
                if count:
                    if mode in {"prefix", "canonical"} and any(previous == 0 for previous in action_counts):
                        continue
                    if mode == "canonical" and any(
                        previous != firefighters for previous in action_counts
                    ):
                        continue
                actions = set(actions_tuple)
                next_defended = defended | actions
                spread = set().union(*(instance.adjacency[v] for v in burned)) - burned - next_defended
                visit(
                    burned | spread,
                    next_defended,
                    t + 1,
                    action_counts + (count,),
                )

    visit(set(instance.initial_fire), set(), 0, ())
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
        base_signature = (encoder.vars.top, encoder.clauses, tuple(assumptions))
        canonical = ActionCanonicalEncoding(encoder, firefighters)
        canonical.apply_mode(mode, horizon)
        return solver.solve(assumptions=assumptions), base_signature, canonical.stats()
    finally:
        encoder.close()
        solver.delete()


def canonicalize_schedule(schedule, firefighters):
    """Test-only operation that moves later actions into earlier free slots."""
    rows = [list(actions) for actions in schedule]
    for t in range(len(rows)):
        while len(rows[t]) < firefighters:
            later = next((s for s in range(t + 1, len(rows)) if rows[s]), None)
            if later is None:
                break
            rows[t].append(rows[later].pop(0))
    while rows and not rows[-1]:
        rows.pop()
    return rows


class ActionCanonicalTests(unittest.TestCase):
    def test_action_indicator_exactly_tracks_nonempty_round(self):
        instance = graph(6, [], (0,))
        distance, _ = preprocess(instance, 6)
        solver = Solver(name="cadical300")
        encoder = Encoder(instance, 6, solver, distance)
        try:
            encoder.ensure_horizon(1)
            canonical = ActionCanonicalEncoding(encoder, 6)
            canonical.add_indicators(1)
            for mask in range(1 << (instance.n - 1)):
                selected = {v for v in range(1, instance.n) if mask >> (v - 1) & 1}
                assumptions = [
                    encoder.a[v, 1] if v in selected else -encoder.a[v, 1]
                    for v in range(instance.n)
                ]
                assumptions.append(canonical.y[1] if selected else -canonical.y[1])
                self.assertTrue(solver.solve(assumptions=assumptions))
                wrong = assumptions[:-1] + [-assumptions[-1]]
                self.assertFalse(solver.solve(assumptions=wrong))
        finally:
            encoder.close()
            solver.delete()

    def test_exhaustive_all_graphs_through_four_vertices(self):
        for n in range(1, 5):
            for instance in all_small_graphs(n):
                for firefighters in range(1, n + 1):
                    base = outcomes(instance, firefighters, "base")
                    results = {mode: outcomes(instance, firefighters, mode) for mode in MODES[1:]}
                    for horizon in range(n + 2):
                        for bound in range(n + 1):
                            base_sat = any(t <= horizon and k <= bound for t, k in base)
                            for mode, attainable in results.items():
                                mode_sat = any(t <= horizon and k <= bound for t, k in attainable)
                                self.assertEqual(
                                    base_sat,
                                    mode_sat,
                                    msg=(
                                        f"mode={mode}, n={n}, B={sorted(instance.initial_fire)}, "
                                        f"D={firefighters}, T={horizon}, K={bound}"
                                    ),
                                )

    def test_sat_encoding_matches_all_modes_on_seeded_random_graphs(self):
        rng = random.Random(92531)
        for sample in range(100):
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
            base_sat, base_signature, _ = sat_result(instance, firefighters, horizon, bound, "base")
            for mode in MODES[1:]:
                mode_sat, signature, _ = sat_result(instance, firefighters, horizon, bound, mode)
                self.assertEqual(base_sat, mode_sat, msg=f"mode={mode}, sample={sample}")
                self.assertEqual(base_signature, signature)

    def test_d1_prefix_and_canonical_are_identical_and_have_expected_size(self):
        instance = graph(1000, [(v, v + 1) for v in range(999)], (0,))
        distance, _ = preprocess(instance, 1)
        stats = {}
        for mode in ("prefix", "canonical"):
            solver = Solver(name="cadical300")
            encoder = Encoder(instance, 1, solver, distance)
            try:
                encoder.ensure_horizon(9)
                assumptions = encoder.assumptions(9, 989, instance.n)
                before_vars, before_clauses = encoder.vars.top, encoder.clauses
                encoding = ActionCanonicalEncoding(encoder, 1)
                encoding.apply_mode(mode, 9)
                stats[mode] = (
                    encoder.vars.top - before_vars,
                    before_clauses,
                    before_clauses + encoding.indicator_clauses + encoding.prefix_clauses,
                    encoding.stats(),
                    tuple(assumptions),
                )
            finally:
                encoder.close()
                solver.delete()
        self.assertEqual(stats["prefix"], stats["canonical"])
        variables, base_clauses, total_clauses, counts, _ = stats["canonical"]
        self.assertEqual(variables, 9)
        self.assertEqual(counts["indicator_clauses"], 9009)
        self.assertEqual(counts["prefix_clauses"], 8)
        self.assertEqual(counts["full_capacity_clauses"], 0)
        self.assertEqual(counts["full_capacity_auxiliary_variables"], 0)
        self.assertEqual(total_clauses - base_clauses, 9017)

    def test_schedule_transformation_preserves_or_improves_simulated_result(self):
        cases = [
            (graph(7, [(v, v + 1) for v in range(6)], (0,)), 1, [[6], [], [5]]),
            (graph(10, [(v, v + 1) for v in range(9)], (0,)), 2, [[9], [], [7, 8]]),
            (graph(12, [(v, v + 1) for v in range(11)], (0,)), 3, [[11, 10], [], [7, 8, 9]]),
        ]
        for instance, firefighters, schedule in cases:
            original = simulate(instance, firefighters, schedule)
            transformed_schedule = canonicalize_schedule(schedule, firefighters)
            transformed = simulate(instance, firefighters, transformed_schedule)
            self.assertLessEqual(transformed.k, original.k)
            self.assertLessEqual(transformed.containment_time, original.containment_time)
            counts = [len(actions) for actions in transformed_schedule]
            positive = [count for count in counts if count]
            self.assertEqual(positive, [firefighters] * (sum(positive) // firefighters) + (
                [sum(positive) % firefighters] if sum(positive) % firefighters else []
            ))

    def test_diagnostic_runs_each_mode_in_a_fresh_solver(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "path.in"
            path.write_text("0\n3\n2\nx\n1\n0\n0 1\n1 2\n")
            base = run_action_canonical_case(path, 1, "cadical300", 1, 2, "base", 5)
            canonical = run_action_canonical_case(path, 1, "cadical300", 1, 2, "canonical", 5)
            self.assertEqual(base["result"], "SAT")
            self.assertEqual(canonical["result"], "SAT")
            self.assertNotEqual(base["worker_pid"], canonical["worker_pid"])
            self.assertEqual(base["base_variables"], canonical["base_variables"])
            self.assertEqual(base["base_clauses"], canonical["base_clauses"])


if __name__ == "__main__":
    unittest.main()
