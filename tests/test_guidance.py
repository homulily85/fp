import tempfile
import unittest
from pathlib import Path

from pysat.solvers import Solver

from ffp_sat.encoder import Encoder
from ffp_sat.guidance import (
    PhaseMode,
    build_phase_literals,
    consensus_phases,
    validate_phase_literals,
)
from ffp_sat.instance import read_instance
from ffp_sat.preprocess import preprocess
from ffp_sat.simulator import simulate


class GuidanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "path.in"
        self.path.write_text("0\n3\n2\nx\n1\n0\n0 1\n1 2\n")
        self.instance = read_instance(self.path)
        self.schedule = ((1,),)

    def tearDown(self):
        self.temp.cleanup()

    def _encoder(self, solver):
        distance, _ = preprocess(self.instance, 1)
        encoder = Encoder(self.instance, 1, solver, distance)
        encoder.ensure_horizon(2)
        assumptions = encoder.assumptions(2, 1, self.instance.n)
        return encoder, assumptions

    def test_action_and_full_phases_follow_simulator_trace(self):
        solver = Solver(name="cadical300")
        encoder, _ = self._encoder(solver)
        try:
            action, _ = build_phase_literals(
                encoder, self.instance, 1, 2, PhaseMode.ACTION, self.schedule
            )
            self.assertEqual(len(action), 6)
            self.assertEqual(sum(literal > 0 for literal in action), 1)
            self.assertEqual(action[1], encoder.a[1, 1])
            full, _ = build_phase_literals(
                encoder, self.instance, 1, 2, PhaseMode.FULL, self.schedule
            )
            self.assertEqual(len(full), 18)
            self.assertEqual(validate_phase_literals(full)["phase_literals"], 18)
            trace_solution = simulate(self.instance, 1, self.schedule)
            self.assertEqual(trace_solution.containment_time, 1)
            self.assertEqual(full[9], -encoder.a[0, 2])  # no action after containment
        finally:
            encoder.close()
            solver.delete()

    def test_consensus_tie_breaks_by_vertex_id(self):
        solver = Solver(name="cadical300")
        encoder, _ = self._encoder(solver)
        try:
            phases, votes = consensus_phases(encoder, [((1,),), ((1,),)], 2)
            self.assertEqual(len(phases), 6)
            self.assertEqual(phases[1], encoder.a[1, 1])
            self.assertEqual(votes[0]["vertices"], [{"vertex": 1, "votes": 2, "fraction": 1.0}])
            self.assertTrue(all(phases[3 + vertex] < 0 for vertex in range(3)))
        finally:
            encoder.close()
            solver.delete()

    def test_phase_modes_preserve_query_semantics_and_formula_size(self):
        signatures = []
        results = []
        for mode in PhaseMode:
            solver = Solver(name="cadical300")
            encoder, assumptions = self._encoder(solver)
            try:
                phases, _ = build_phase_literals(
                    encoder,
                    self.instance,
                    1,
                    2,
                    mode,
                    self.schedule,
                    [self.schedule],
                )
                if mode is not PhaseMode.NONE:
                    solver.set_phases(phases)
                signatures.append((encoder.vars.top, encoder.clauses, tuple(assumptions)))
                results.append(solver.solve(assumptions=assumptions))
            finally:
                encoder.close()
                solver.delete()
        self.assertEqual(len(set(signatures)), 1)
        self.assertEqual(results, [True] * len(PhaseMode))


if __name__ == "__main__":
    unittest.main()
