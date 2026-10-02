import itertools
import random
import time
import unittest

from pysat.solvers import Solver

from ffp_sat.encoder import Encoder
from ffp_sat.heuristic import dominates, threat
from ffp_sat.preprocess import preprocess
from ffp_sat.simulator import Solution, simulate
from ffp_sat.stk_solver import initial_horizon, next_horizon, search
from ffp_sat.totalizer import AtMost
from ffp_sat.variables import VarManager

from .helpers import brute_force, graph, statistics


class SATTests(unittest.TestCase):
    def test_totalizer_exhaustive_and_ids(self):
        manager = VarManager()
        for size in range(1, 7):
            literals = [manager.new() for _ in range(size)]
            tree = AtMost(literals, size - 1, manager)
            self.assertGreater(manager.new(), tree.tree.top_id)
            with Solver(name="cadical300", bootstrap_with=tree.clauses) as solver:
                for values in itertools.product((False, True), repeat=size):
                    assignment = [v if value else -v for v, value in zip(literals, values)]
                    for k in range(size + 1):
                        bound = tree.assumption(k)
                        self.assertEqual(
                            solver.solve(assumptions=assignment + ([bound] if bound else [])),
                            sum(values) <= k,
                        )
            tree.close()
        tree = AtMost([], 0, manager)
        self.assertIsNone(tree.assumption(0))
        tree.close()

    def test_no_spontaneous_burning(self):
        instance = graph(3, [])
        with Solver(name="cadical300") as solver:
            encoder = Encoder(instance, 1, solver, preprocess(instance, 1)[0])
            encoder.ensure_horizon(2)
            self.assertFalse(solver.solve(assumptions=[encoder.b[1, 1]]))
            self.assertTrue(solver.solve(assumptions=encoder.assumptions(2, 1, 3)))
            encoder.close()

    def test_activation_and_extension(self):
        # A spreading path is not stable at T=2, but is stable at T=3.
        instance = graph(4, [(0, 1), (1, 2), (2, 3)])
        with Solver(name="cadical300") as solver:
            encoder = Encoder(instance, 1, solver, preprocess(instance, 1)[0])
            encoder.ensure_horizon(2)
            no_defense = [-a for a in encoder.a.values()]
            self.assertFalse(solver.solve(assumptions=encoder.assumptions(2, 4, 4) + no_defense))
            encoder.ensure_horizon(3)
            no_defense = [-a for a in encoder.a.values()]
            self.assertTrue(solver.solve(assumptions=encoder.assumptions(3, 4, 4) + no_defense))
            self.assertFalse(solver.solve(assumptions=encoder.assumptions(2, 4, 4) + no_defense))
            count = encoder.clauses
            encoder.ensure_horizon(3)
            self.assertEqual(encoder.clauses, count)
            self.assertEqual(encoder.extensions, 2)
            encoder.close()

    def test_horizon_zero_containment(self):
        isolated = graph(2, [])
        path = graph(2, [(0, 1)])
        with Solver(name="cadical300") as solver:
            encoder = Encoder(isolated, 1, solver, preprocess(isolated, 1)[0])
            self.assertTrue(solver.solve(assumptions=encoder.assumptions(0, 1, 2)))
            encoder.close()
        with Solver(name="cadical300") as solver:
            encoder = Encoder(path, 1, solver, preprocess(path, 1)[0])
            self.assertFalse(solver.solve(assumptions=encoder.assumptions(0, 2, 2)))
            encoder.close()

    def test_fixed_actions_match_simulator(self):
        instance = graph(5, [(0, 1), (1, 2), (2, 3), (0, 4)])
        schedule = [(4,), (2,), ()]
        expected = simulate(instance, 1, schedule)
        with Solver(name="cadical300") as solver:
            encoder = Encoder(instance, 1, solver, preprocess(instance, 1)[0])
            encoder.ensure_horizon(3)
            actions = [a if v in schedule[t - 1] else -a for (v, t), a in encoder.a.items()]
            self.assertTrue(solver.solve(assumptions=encoder.assumptions(3, 5, 5) + actions))
            positive = set(solver.get_model())
            actual = {v for v in range(5) if encoder.b[v, 3] in positive}
            self.assertEqual(actual, expected.burned)
            encoder.close()

    def test_horizon_queries_against_oracle(self):
        rng = random.Random(50)
        for _ in range(20):
            n = rng.randint(3, 6)
            instance = graph(n, [(u, v) for u in range(n) for v in range(u + 1, n) if rng.random() < 0.4])
            d = rng.randint(1, 3)
            with Solver(name="cadical300") as solver:
                encoder = Encoder(instance, d, solver, preprocess(instance, d)[0])
                previous = [False] * (n + 1)
                for t in range(0, n + 1):
                    encoder.ensure_horizon(t)
                    optimum = brute_force(instance, d, t)
                    current = []
                    for k in range(n + 1):
                        sat = solver.solve(assumptions=encoder.assumptions(t, k, n))
                        self.assertEqual(sat, k >= optimum)
                        self.assertFalse(previous[k] and not sat)
                        current.append(sat)
                    previous = current
                encoder.close()

    def check_optimum(self, instance, d):
        distance, lower = preprocess(instance, d)
        best = threat(instance, d)
        bounds = []

        def record(solution, lb, metrics):
            bounds.append((lb, metrics.get("certifying", False), metrics.get("update_source")))
            if metrics.get("update_source") == "SAT_QUERY":
                self.assertEqual(metrics["current_k_bound"], solution.k - 1)

        result, lb = search(
            instance,
            d,
            best,
            lower,
            distance,
            "cadical300",
            time.monotonic() + 10,
            record,
            statistics(),
        )
        optimum = brute_force(instance, d)
        self.assertEqual((result.k, lb), (optimum, optimum))
        checked = simulate(instance, d, result.schedule)
        self.assertEqual((checked.k, checked.burned), (result.k, result.burned))
        self.assertLessEqual(checked.containment_time, result.containment_time)
        for lb, certifying, source in bounds:
            if source != "UNSAT" or not certifying:
                self.assertEqual(lb, lower)

    def test_short_unsat_does_not_raise_global_lower_bound(self):
        instance = graph(8, [
            (0, 1), (0, 2), (0, 4), (0, 6), (1, 4), (1, 7), (2, 3),
            (2, 5), (2, 7), (3, 7), (4, 5), (4, 7), (6, 7),
        ])
        distance, lower = preprocess(instance, 1)
        initial = threat(instance, 1)
        events = []
        stats = statistics()
        best, final_lower = search(
            instance,
            1,
            initial,
            lower,
            distance,
            "cadical300",
            time.monotonic() + 5,
            lambda s, lb, st: events.append((lb, st["current_t"], st["unsat_results"])),
            stats,
            initial_horizon_factor=1.0,
        )
        first_unsat = next(event for event in events if event[2] == 1)
        self.assertEqual(first_unsat[0], lower)
        self.assertEqual(best.k, final_lower)
        self.assertGreaterEqual(stats["number_of_horizon_extensions"], 2)

    def test_pareto_dominance_uses_one_strict_coordinate(self):
        early = Solution((), frozenset({0}), frozenset(), 1)
        later_same_k = Solution((), frozenset({0}), frozenset(), 2)
        same_time_more_burned = Solution((), frozenset({0, 1}), frozenset(), 1)
        self.assertTrue(dominates(early, later_same_k))
        self.assertTrue(dominates(early, same_time_more_burned))
        self.assertFalse(dominates(early, Solution((), frozenset({0}), frozenset(), 1)))

    def test_next_horizon_always_advances(self):
        self.assertEqual(next_horizon(0, 10), 1)
        self.assertEqual(next_horizon(2, 10), 4)
        self.assertEqual(next_horizon(7, 10), 10)

    def test_initial_horizon_scales_with_ceiling(self):
        self.assertEqual(initial_horizon(1, 10), 2)
        self.assertEqual(initial_horizon(3, 10), 5)
        self.assertEqual(initial_horizon(8, 10), 10)
        self.assertEqual(initial_horizon(0, 10), 0)
        self.assertEqual(initial_horizon(2, 10, factor=2.0), 4)
        self.assertEqual(initial_horizon(3, 10, factor=1.25), 4)

    def test_all_graphs_up_to_four(self):
        for n in range(1, 5):
            edges = list(itertools.combinations(range(n), 2))
            for bits in range(1 << len(edges)):
                instance = graph(n, [edge for i, edge in enumerate(edges) if (bits >> i) & 1])
                for d in range(1, min(3, n) + 1):
                    self.check_optimum(instance, d)

    def test_one_hundred_random_graphs(self):
        rng = random.Random(2026)
        for _ in range(100):
            n = rng.randint(5, 8)
            edges = [(u, v) for u in range(n) for v in range(u + 1, n) if rng.random() < 0.35]
            fire = rng.sample(range(n), rng.randint(1, min(3, n)))
            self.check_optimum(graph(n, edges, fire), rng.randint(1, 3))
        self.check_optimum(graph(5, [], range(5)), 1)
        self.check_optimum(graph(5, [(0, 1)]), 10)
