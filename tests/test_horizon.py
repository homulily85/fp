import itertools
import random
import time
import unittest
from dataclasses import replace
from unittest.mock import Mock, patch

from ffp_sat.heuristic import threat
from ffp_sat.horizon import compute_horizon_bounds, initial_horizon
from ffp_sat.instance import read_instance
from ffp_sat.preprocess import preprocess
from ffp_sat.simulator import Solution, simulate
from ffp_sat.stk_solver import search
from ffp_sat.worker import worker

from .helpers import brute_force, graph, statistics
from .test_process import Collector


class PolicyEncoder:
    """Record policy decisions without allocating a real large CNF."""

    def __init__(self, instance, d, solver, distance):
        self.instance = instance
        self.horizon = 0
        self.b, self.d = {}, {}
        self.targets = []

    def ensure_horizon(self, t):
        assert t >= self.horizon
        self.targets.append(t)
        self.horizon = t
        for v in range(self.instance.n):
            self.b[v, t] = t * self.instance.n + v + 1
            self.d[v, t] = 10000000 + t * self.instance.n + v + 1

    def assumptions(self, t, k, upper):
        return []

    def decode(self, model, t):
        return []

    def stats(self):
        return {}

    def close(self):
        pass


class HorizonTests(unittest.TestCase):
    def test_exact_bounds_and_invalid_inputs(self):
        bounds = compute_horizon_bounds(50, 1, 1, 5, 42)
        self.assertEqual(
            (bounds.old_safe, bounds.structural, bounds.incumbent,
             bounds.lower_bound, bounds.certification),
            (50, 25, 41, 46, 25),
        )
        bounds = compute_horizon_bounds(1000, 1, 1, 5, 992)
        self.assertEqual(bounds.stats()["t_cert"], 500)
        self.assertEqual(bounds.lower_bound, 996)
        self.assertEqual(compute_horizon_bounds(1000, 1, 1, 5, 990).certification, 500)
        self.assertEqual(compute_horizon_bounds(5, 5, 10, 5, 5).certification, 0)
        # The floor + 1 is essential when n-L is divisible by D.
        self.assertEqual(compute_horizon_bounds(10, 1, 3, 4, 8).lower_bound, 3)
        for args in ((5, 0, 1, 1, 2), (5, 1, 0, 1, 2), (5, 1, 1, 3, 2)):
            with self.assertRaises(ValueError):
                compute_horizon_bounds(*args)
        for factor in (0.5, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                initial_horizon(5, 25, factor)

    def run_policy(self, n, upper, containment, lower, outcomes, factor=1.5):
        instance = graph(n, [])
        best = Solution((), frozenset(range(upper)), frozenset(), containment)
        solver = Mock()
        solver.solve.side_effect = [sat for sat, k in outcomes]
        encoder = PolicyEncoder(instance, 1, solver, [])
        sat_counts = iter(k for sat, k in outcomes if sat)
        solver.get_model.side_effect = lambda: [
            encoder.b[v, encoder.horizon] for v in range(next(sat_counts))
        ]
        stats = statistics()
        events = []
        with patch("ffp_sat.stk_solver.Encoder", return_value=encoder) as constructor:
            result, lb = search(
                instance, 1, best, lower, [], "cadical300", time.monotonic() + 5,
                lambda s, lb, st: events.append(dict(st, lb=lb)), stats,
                solver_instance=solver, initial_horizon_factor=factor,
            )
        self.assertEqual(constructor.call_count, 1)
        return result, lb, stats, encoder.targets, events

    def test_unsat_sequences_and_large_instance_cap(self):
        for factor, expected in ((1, [5, 10, 20, 25]), (1.5, [8, 16, 25])):
            result, lb, stats, targets, _ = self.run_policy(
                50, 42, 5, 5, [(False, None)] * len(expected), factor,
            )
            self.assertEqual(targets, expected)
            self.assertEqual((result.k, lb, stats["max_encoded_t"]), (42, 42, 25))
        _, _, stats, targets, _ = self.run_policy(
            1000, 992, 7, 5, [(True, 990)] + [(False, None)] * 7,
        )
        self.assertEqual(max(targets), 500)
        self.assertEqual(stats["query_t_cert"], 500)

    def test_shrinking_certification_keeps_current_horizon(self):
        result, lb, stats, targets, events = self.run_policy(
            30, 20, 7, 2, [(True, 5), (False, None)],
        )
        self.assertEqual(targets, [11, 11])
        improved = next(e for e in events if e["update_source"] == "SAT")
        self.assertEqual(improved["t_cert"], 4)
        self.assertEqual((result.k, lb), (5, 5))
        self.assertTrue(stats["certifying"])

    def test_early_optimum_does_not_construct_solver(self):
        started = time.monotonic()
        collector = Collector()
        with patch("ffp_sat.worker.Solver", side_effect=AssertionError("unnecessary solver")):
            worker(
                collector, "dataset/50_ep0.1_0_gilbert_1.in", 100,
                dict(solver="cadical300", seed=0, heuristic_budget=0.01),
                started, started + 5,
            )
        self.assertEqual(collector.messages[-1][0], "FINAL")
        self.assertEqual(collector.messages[-1][1]["status"], "OPTIMAL")
        self.assertEqual(collector.messages[-1][1]["max_encoded_t"], 0)

    def test_real_fifty_vertex_regression(self):
        instance = read_instance("dataset/50_ep0.1_0_gilbert_1.in")
        # A known incumbent avoids dependence on portfolio wall-clock budget.
        best = simulate(instance, 1, [[39], [13], [16], [46], [29]])
        self.assertEqual((best.k, best.containment_time), (42, 5))
        distance, lower = preprocess(instance, 1)
        stats = statistics()
        result, lb = search(
            instance, 1, best, lower, distance, "cadical300", time.monotonic() + 30,
            lambda *args: None, stats,
        )
        self.assertEqual((result.k, lb), (42, 42))
        self.assertLessEqual(stats["max_encoded_t"], 25)
        self.assertEqual(simulate(instance, 1, result.schedule).k, 42)

    def assert_bounds(self, instance, d):
        optimum = brute_force(instance, d)
        tmin = next(t for t in range(instance.n + 1) if brute_force(instance, d, t) == optimum)
        b0 = len(instance.initial_fire)
        for upper in range(optimum + 1, instance.n + 1):
            for lower in range(b0, optimum + 1):
                bounds = compute_horizon_bounds(instance.n, b0, d, lower, upper)
                self.assertLessEqual(tmin, bounds.structural)
                self.assertLessEqual(tmin, bounds.incumbent)
                self.assertLessEqual(tmin, bounds.lower_bound)
                self.assertLessEqual(tmin, bounds.certification)
        self.assertLessEqual(tmin, compute_horizon_bounds(
            instance.n, b0, d, optimum, optimum,
        ).structural)

    def test_bounds_against_independent_oracle(self):
        for n in range(1, 5):
            edges = list(itertools.combinations(range(n), 2))
            for mask in range(1 << len(edges)):
                instance = graph(n, [e for i, e in enumerate(edges) if mask >> i & 1])
                for d in range(1, n + 1):
                    self.assert_bounds(instance, d)
        rng = random.Random(1003)
        for _ in range(40):
            n = rng.randint(5, 8)
            instance = graph(
                n, [e for e in itertools.combinations(range(n), 2) if rng.random() < 0.4],
                rng.sample(range(n), rng.randint(1, n)),
            )
            self.assert_bounds(instance, rng.randint(1, n))

    def test_old_new_and_oracle_agree(self):
        rng = random.Random(30)
        for _ in range(30):
            n = rng.randint(4, 8)
            instance = graph(
                n, [e for e in itertools.combinations(range(n), 2) if rng.random() < 0.4],
                rng.sample(range(n), rng.randint(1, 3)),
            )
            d = rng.randint(1, 3)
            distance, lower = preprocess(instance, d)
            best = threat(instance, d)
            optimum = brute_force(instance, d)

            def old_policy(*args):
                bounds = compute_horizon_bounds(*args)
                return replace(bounds, certification=bounds.old_safe)

            for policy in (compute_horizon_bounds, old_policy):
                with patch("ffp_sat.stk_solver.compute_horizon_bounds", side_effect=policy):
                    result, lb = search(
                        instance, d, best, lower, distance, "cadical300",
                        time.monotonic() + 5, lambda *args: None, statistics(),
                    )
                self.assertEqual((result.k, lb), (optimum, optimum))
