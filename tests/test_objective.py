import itertools
import random
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from pysat.formula import CNF
from pysat.solvers import Solver

from ffp_sat.encoder import Encoder
from ffp_sat.heuristic import threat
from ffp_sat.instance import read_instance
from ffp_sat.objective import IncrementalAtLeastCounter, ObjectiveManager
from ffp_sat.preprocess import preprocess
from ffp_sat.stk_solver import search
from ffp_sat.totalizer import AtMost, IncrementalAtMost
from ffp_sat.variables import VarManager

from .helpers import brute_force, graph, statistics


class LegacyEncoder(Encoder):
    """Test-only reference for the previous eager/burned objective encoding."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.ensure_containment(0)

    def ensure_horizon(self, target):
        super().ensure_horizon(target)
        for t in range(target + 1):
            self.ensure_containment(t)

    def assumptions(self, t, k, upper):
        if t not in self.objectives:
            tree = AtMost([self.b[v, t] for v in range(self.instance.n)], upper, self.vars)
            self.objectives[t] = tree
            for clause in tree.clauses:
                self.add(clause, "objective_totalizer")
        bound = self.objectives[t].assumption(k)
        return [self.h[t]] + ([bound] if bound is not None else [])


class ObjectiveTests(unittest.TestCase):
    def test_saved_counter_exhaustive_equivalence(self):
        for n in range(9):
            manager = VarManager()
            ids = [manager.new() for _ in range(n)]
            literals = [v if i % 2 else -v for i, v in enumerate(ids)]
            with Solver(name="cadical300") as solver:
                counter = IncrementalAtLeastCounter(literals, manager, solver.add_clause)
                for q in range(n + 1):
                    counter.ensure_threshold(q)
                    top, clauses = manager.top, counter.number_of_clauses
                    output = counter.assumption(q)
                    self.assertEqual((manager.top, counter.number_of_clauses), (top, clauses))
                    for values in itertools.product((False, True), repeat=n):
                        assignment = [lit if value else -lit for lit, value in zip(literals, values)]
                        positive = [output] if output is not None else []
                        self.assertEqual(solver.solve(assumptions=assignment + positive), sum(values) >= q)
                        if output is not None:
                            self.assertEqual(
                                solver.solve(assumptions=assignment + [-output]), sum(values) < q
                            )
                for invalid in (-1, n + 1):
                    with self.assertRaises(ValueError):
                        counter.assumption(invalid)

    def test_direct_incremental_and_interleaved_ids(self):
        for direct in (False, True):
            manager = VarManager(capture_names=True)
            literals = [manager.new() for _ in range(5)]
            with Solver(name="cadical300") as solver:
                counter = IncrementalAtLeastCounter(literals, manager, solver.add_clause, horizon=9)
                if not direct:
                    counter.ensure_threshold(1)
                semantic = manager.new("extra")
                activation = manager.new("h", activation=True)
                burned = IncrementalAtMost(literals, manager, solver.add_clause)
                burned.ensure_bound(0)
                old_output = dict(counter.output)
                counter.ensure_threshold(3)
                self.assertEqual({q: counter.output[q] for q in old_output}, old_output)
                self.assertNotIn(semantic, counter.states.values())
                self.assertNotIn(activation, counter.states.values())
                self.assertTrue(all(i >= j for i, j in counter.states))
                self.assertTrue(all(manager.names[v].startswith("c[9,") for v in counter.states.values()))
                burned.ensure_bound(3)
                self.assertEqual(manager.top, manager.semantic + manager.activation + manager.auxiliary)
                self.assertEqual(manager.semantic, len(literals) + 1)
                self.assertEqual(manager.activation, 1)
                self.assertEqual(manager.auxiliary, counter.auxiliary_variables + burned.auxiliary_variables)
                for values in itertools.product((False, True), repeat=5):
                    assignment = [v if value else -v for v, value in zip(literals, values)]
                    self.assertEqual(
                        solver.solve(assumptions=assignment + [counter.assumption(3)]), sum(values) >= 3
                    )
                    self.assertEqual(
                        solver.solve(assumptions=assignment + [burned.assumption(3)]), sum(values) <= 3
                    )
                burned.close()

    def test_objective_switch_and_unassumed_outputs(self):
        for n in range(1, 8):
            manager = VarManager()
            literals = [manager.new() for _ in range(n)]
            with Solver(name="cadical300") as solver:
                objective = ObjectiveManager(
                    literals, manager, lambda clause, group: solver.add_clause(clause), 2
                )
                sequence = [n - 1, 0, n // 2, n - 1, *range(n + 1)]
                for k in sequence:
                    output = objective.assumption(k)
                    self.assertEqual(
                        objective.current["side"], "none" if k >= n else "saved" if k > n // 2 else "burned"
                    )
                    for values in itertools.product((False, True), repeat=n):
                        assignment = [v if value else -v for v, value in zip(literals, values)]
                        self.assertTrue(solver.solve(assumptions=assignment))
                        self.assertEqual(
                            solver.solve(assumptions=assignment + ([output] if output else [])),
                            sum(values) <= k,
                        )
                self.assertIsNone(objective.assumption(n + 1))
                with self.assertRaises(ValueError):
                    objective.assumption(-1)
                objective.close()

    def test_lazy_containment_and_horizon_caches(self):
        instance = graph(5, [(0, 1), (1, 2), (2, 3), (3, 4)])
        with Solver(name="cadical300") as solver:
            encoder = Encoder(instance, 1, solver, preprocess(instance, 1)[0], debug=True)
            encoder.ensure_horizon(9)
            self.assertFalse(encoder.objectives)
            self.assertEqual(encoder.vars.activation, 0)
            self.assertEqual(encoder.debug_profile()["clauses"]["containment"]["number_of_clauses"], 0)
            activation = encoder.ensure_containment(9)
            count, top = encoder.clauses, encoder.vars.top
            self.assertEqual(encoder.ensure_containment(9), activation)
            self.assertEqual((encoder.clauses, encoder.vars.top), (count, top))
            first = encoder.assumptions(9, 4, 5)
            encoder.ensure_horizon(18)
            self.assertEqual(encoder.debug_profile()["containment"]["number_of_clauses"], 2 * instance.m)
            second = encoder.assumptions(18, 4, 5)
            self.assertIn(-activation, second)
            self.assertNotEqual(
                encoder.objectives[9].saved_counter.output[1], encoder.objectives[18].saved_counter.output[1]
            )
            self.assertEqual(encoder.debug_profile()["containment"]["number_of_clauses"], 4 * instance.m)
            self.assertIn(-encoder.h[18], encoder.assumptions(9, 4, 5))
            self.assertTrue(solver.solve(assumptions=first))
            for t in (-1, 19):
                with self.assertRaises(ValueError):
                    encoder.ensure_containment(t)
            encoder.close()

    def test_queries_match_legacy_and_bruteforce(self):
        rng = random.Random(12026)
        for _ in range(15):
            n = rng.randint(2, 6)
            edges = [edge for edge in itertools.combinations(range(n), 2) if rng.random() < 0.4]
            instance = graph(n, edges, rng.sample(range(n), rng.randint(1, min(n, 3))))
            d = rng.randint(1, 3)
            with Solver(name="cadical300") as new_solver, Solver(name="cadical300") as old_solver:
                new = Encoder(instance, d, new_solver, preprocess(instance, d)[0])
                old = LegacyEncoder(instance, d, old_solver, preprocess(instance, d)[0])
                for t in range(n + 1):
                    new.ensure_horizon(t)
                    old.ensure_horizon(t)
                    optimum = brute_force(instance, d, t)
                    for k in range(n + 1):
                        actual = new_solver.solve(assumptions=new.assumptions(t, k, n))
                        self.assertEqual(actual, old_solver.solve(assumptions=old.assumptions(t, k, n)))
                        self.assertEqual(actual, k >= optimum)
                new.close()
                old.close()

    def test_stk_optimum_matches_legacy(self):
        rng = random.Random(23026)
        for _ in range(20):
            n = rng.randint(3, 7)
            instance = graph(n, [edge for edge in itertools.combinations(range(n), 2) if rng.random() < 0.4])
            d = rng.randint(1, 2)
            distance, lower = preprocess(instance, d)
            incumbent = threat(instance, d)
            optimum = brute_force(instance, d)
            for cls in (LegacyEncoder, Encoder):
                with patch("ffp_sat.stk_solver.Encoder", cls):
                    best, lb = search(
                        instance,
                        d,
                        incumbent,
                        lower,
                        distance,
                        "cadical300",
                        time.monotonic() + 10,
                        lambda *args: None,
                        statistics(),
                    )
                self.assertEqual((best.k, lb), (optimum, optimum))

    def test_large_instance_structure(self):
        instance = read_instance("dataset/1000_ep0.0075_0_gilbert_1.in")
        with Solver(name="cadical300") as solver:
            encoder = Encoder(instance, 1, solver, preprocess(instance, 1)[0], debug=True)
            encoder.ensure_horizon(9)
            for k in (991, 990, 989):
                encoder.assumptions(9, k, 992)
                counter = encoder.objectives[9].saved_counter
                self.assertEqual(counter.maximum, instance.n - k)
            profile = encoder.debug_profile()
            self.assertEqual(
                profile["containment"],
                dict(queried_horizons=[9], activation_variables=1, number_of_clauses=2 * instance.m),
            )
            self.assertEqual(profile["objective"]["saved_threshold"], 11)
            manager = VarManager()
            literals = [manager.new() for _ in range(instance.n)]
            reference = AtMost(literals, 992, manager)
            self.assertLess(counter.number_of_clauses, len(reference.clauses) / 5)
            reference.close()
            encoder.close()

    def test_named_cnf_saved_counter(self):
        instance = graph(5, [(0, 1), (1, 2)])
        with Solver(name="cadical300") as solver, tempfile.TemporaryDirectory() as root:
            encoder = Encoder(instance, 1, solver, preprocess(instance, 1)[0], capture_cnf=True)
            encoder.ensure_horizon(2)
            assumptions = encoder.assumptions(2, 4, 5)
            paths = encoder.export_dimacs(Path(root) / "query", assumptions, 2, 4)
            self.assertIn("c[2,", Path(paths["named"]).read_text())
            for path in paths.values():
                with Solver(name="cadical300", bootstrap_with=CNF(from_file=path).clauses) as exported:
                    self.assertEqual(exported.solve(), solver.solve(assumptions=assumptions))
            encoder.close()

    def test_profile_totals_after_objective_switches(self):
        instance = graph(5, [(0, 1), (1, 2)])
        with Solver(name="cadical300") as solver:
            encoder = Encoder(instance, 1, solver, preprocess(instance, 1)[0], debug=True)
            encoder.ensure_horizon(2)
            for k in (4, 0, 2, 3):
                encoder.assumptions(2, k, 5)
            profile = encoder.debug_profile()
            groups = profile["clauses"]
            self.assertEqual(sum(g["number_of_clauses"] for g in groups.values()), encoder.clauses)
            self.assertEqual(sum(g["auxiliary_variables"] for g in groups.values()), encoder.vars.auxiliary)
            self.assertEqual(sum(profile["clause_length_histogram"].values()), encoder.clauses)
            totalizers = sum(
                groups[g]["number_of_clauses"] for g in ("firefighter_totalizer", "objective_totalizer")
            )
            saved = groups["objective_saved_counter"]["number_of_clauses"]
            self.assertEqual(profile["totalizer_clause_ratio"], totalizers / encoder.clauses)
            self.assertEqual(profile["cardinality_clause_ratio"], (totalizers + saved) / encoder.clauses)
            self.assertEqual(profile["objective"]["side"], "saved")
            self.assertEqual(profile["objective"]["saved_threshold"], 2)
            encoder.close()
