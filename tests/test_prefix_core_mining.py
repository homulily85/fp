import tempfile
import unittest
from itertools import combinations
from pathlib import Path

from pysat.solvers import Solver

from ffp_sat.encoder import Encoder
from ffp_sat.prefix_core_mining import (
    guarded_core_clause,
    non_subsumed_cores,
    run_prefix_probe,
    semantic_core,
    semantic_prefix,
    summarize_cores,
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


class PrefixCoreMiningTests(unittest.TestCase):
    def setUp(self):
        self.instance = graph(3, [(0, 1), (1, 2)])
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "path.in"
        write_instance(self.path, self.instance)

    def tearDown(self):
        self.temp.cleanup()

    def test_cadical_assumption_core_is_a_subset_of_assumptions(self):
        with Solver(name="cadical300", bootstrap_with=[[-1, -2]]) as solver:
            self.assertFalse(solver.solve(assumptions=[1, 2]))
            core = solver.get_core()
        self.assertIsNotNone(core)
        self.assertTrue(set(core).issubset({1, 2}))

    def test_probe_gets_only_prefix_literals_in_core_and_guard_preserves_query(self):
        probe = run_prefix_probe(
            self.path,
            firefighters=1,
            solver_name="cadical300",
            horizon=2,
            bound=1,
            prefix=[[1, 2]],
            timeout=10,
        )
        self.assertEqual(probe["result"], "UNSAT")
        self.assertEqual(probe["core"], [[1, 2, True]])
        self.assertTrue(set(probe["raw_core"]).issubset(probe["prefix_literals"]))

        distance, _ = preprocess(self.instance, 1)
        with Solver(name="cadical300") as core_solver:
            core_encoder = Encoder(self.instance, 1, core_solver, distance)
            try:
                core_encoder.ensure_horizon(2)
                query = core_encoder.assumptions(2, 1, self.instance.n)
                for literal in query:
                    core_solver.add_clause([literal])
                self.assertFalse(core_solver.solve(assumptions=[core_encoder.a[2, 1]]))
            finally:
                core_encoder.close()

        with Solver(name="cadical300") as solver:
            encoder = Encoder(self.instance, 1, solver, distance)
            try:
                encoder.ensure_horizon(2)
                assumptions = encoder.assumptions(2, 1, self.instance.n)
                self.assertTrue(solver.solve(assumptions=assumptions))
                clause = guarded_core_clause(assumptions, [(1, 2, True)], encoder)
                solver.add_clause(clause)
                self.assertTrue(solver.solve(assumptions=assumptions))
            finally:
                encoder.close()

    def test_semantic_prefix_and_core_subsumption(self):
        self.assertEqual(semantic_prefix([[5], [9], [3]], 2), ((1, 5), (2, 9)))
        cores = [
            [[1, 5, True], [2, 9, True]],
            [[1, 5, True]],
            [[1, 5, True], [2, 9, True]],
            [[3, 12, True]],
        ]
        result = non_subsumed_cores(cores)
        self.assertEqual(result, [((1, 5, True),), ((3, 12, True),)])
        summary = summarize_cores(cores)
        self.assertEqual(summary["raw_cores"], 4)
        self.assertEqual(summary["unique_cores"], 3)
        self.assertEqual(summary["non_subsumed_cores"], 2)
        self.assertEqual(summary["action_frequency"][0]["count"], 3)

    def test_semantic_core_rejects_non_assumption_literal(self):
        distance, _ = preprocess(self.instance, 1)
        with Solver(name="cadical300") as solver:
            encoder = Encoder(self.instance, 1, solver, distance)
            try:
                encoder.ensure_horizon(2)
                action = encoder.a[2, 1]
                with self.assertRaises(ValueError):
                    semantic_core([action], encoder, [-action])
            finally:
                encoder.close()

    def test_guarded_core_preserves_query_on_all_graphs_through_three_vertices(self):
        for n in range(1, 4):
            possible_edges = list(combinations(range(n), 2))
            for edge_mask in range(1 << len(possible_edges)):
                edges = [
                    edge
                    for index, edge in enumerate(possible_edges)
                    if edge_mask & (1 << index)
                ]
                for fire_mask in range(1, 1 << n):
                    fire = tuple(v for v in range(n) if fire_mask & (1 << v))
                    instance = graph(n, edges, fire)
                    for firefighters in range(1, n + 1):
                        distance, _ = preprocess(instance, firefighters)
                        for bound in range(len(fire), n + 1):
                            master = Solver(name="cadical300")
                            master_encoder = Encoder(instance, firefighters, master, distance)
                            probe_solver = Solver(name="cadical300")
                            probe_encoder = Encoder(instance, firefighters, probe_solver, distance)
                            try:
                                for encoder in (master_encoder, probe_encoder):
                                    encoder.ensure_horizon(1)
                                query = master_encoder.assumptions(1, bound, n)
                                probe_query = probe_encoder.assumptions(1, bound, n)
                                self.assertEqual(query, probe_query)
                                base_sat = master.solve(assumptions=query)
                                for literal in probe_query:
                                    probe_solver.add_clause([literal])
                                found_core = False
                                for vertex in range(n):
                                    prefix_literal = probe_encoder.a[vertex, 1]
                                    if probe_solver.solve(assumptions=[prefix_literal]):
                                        continue
                                    raw = probe_solver.get_core()
                                    if raw is None:
                                        self.assertFalse(probe_solver.solve())
                                        core = ()
                                    else:
                                        self.assertTrue(set(raw).issubset({prefix_literal}))
                                        core = semantic_core(raw, probe_encoder, [prefix_literal])
                                    blocking = guarded_core_clause(query, core, master_encoder)
                                    mapped_core = [
                                        master_encoder.a[v, t] if positive else -master_encoder.a[v, t]
                                        for t, v, positive in core
                                    ]
                                    self.assertFalse(
                                        master.solve(assumptions=[*query, *mapped_core])
                                    )
                                    master.add_clause(blocking)
                                    self.assertEqual(master.solve(assumptions=query), base_sat)
                                    found_core = True
                                    break
                                self.assertTrue(found_core)
                            finally:
                                master_encoder.close()
                                probe_encoder.close()
                                master.delete()
                                probe_solver.delete()


if __name__ == "__main__":
    unittest.main()
