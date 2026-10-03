import csv
import json
import tempfile
import unittest
from pathlib import Path

from ffp_sat.diagnose import _write_outputs, run_case, run_phase_case


class DiagnoseTests(unittest.TestCase):
    def test_heatmap_cases_use_fresh_workers_and_contained_schedules(self):
        with tempfile.TemporaryDirectory() as root:
            instance = Path(root) / "path.in"
            instance.write_text("0\n3\n2\nx\n1\n0\n0 1\n1 2\n")
            at_zero = run_case(instance, 1, "cadical300", 0, 3, [], 5)
            at_one = run_case(instance, 1, "cadical300", 1, 2, [], 5)
            self.assertEqual(at_zero["result"], "UNSAT")
            self.assertEqual(at_one["result"], "SAT")
            self.assertNotEqual(at_zero["worker_pid"], at_one["worker_pid"])
            self.assertLessEqual(at_one["actual_k"], 2)
            self.assertLessEqual(at_one["containment_time"], 1)
            self.assertEqual(at_zero["containment_clauses"], 4)
            self.assertEqual(at_one["containment_clauses"], 4)
            self.assertEqual(at_one["containment_activation_variables"], 1)

    def test_case_timeout_is_not_reported_as_unsat(self):
        with tempfile.TemporaryDirectory() as root:
            instance = Path(root) / "path.in"
            instance.write_text("0\n3\n2\nx\n1\n0\n0 1\n1 2\n")
            result = run_case(instance, 1, "cadical300", 2, 2, [], 0.001)
            self.assertEqual(result["result"], "TIMEOUT")
            self.assertEqual(result["solve_time"], 0.0)

    def test_json_and_csv_outputs(self):
        case = {
            "experiment": "horizon_heatmap",
            "T": 3,
            "K": 2,
            "result": "SAT",
            "schedule": [[1]],
            "fixed_actions": [],
        }
        with tempfile.TemporaryDirectory() as root:
            json_path, csv_path = _write_outputs(
                Path(root), "horizon_heatmap", [case], {"instance": "path.in"}
            )
            self.assertEqual(json.loads(json_path.read_text())["cases"], [case])
            with csv_path.open(newline="") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(rows[0]["result"], "SAT")
            self.assertEqual(json.loads(rows[0]["schedule"]), [[1]])

    def test_phase_replay_uses_prior_bounds_then_reports_final_query(self):
        with tempfile.TemporaryDirectory() as root:
            instance = Path(root) / "path.in"
            instance.write_text("0\n3\n2\nx\n1\n0\n0 1\n1 2\n")
            result = run_phase_case(
                instance,
                1,
                "cadical300",
                2,
                1,
                "action",
                [[1]],
                [[[1]]],
                2,
                0,
                5,
                replay_bounds=[2],
            )
            self.assertEqual(result["result"], "SAT")
            self.assertEqual(result["replay_results"][0]["K"], 2)
            self.assertEqual(result["replay_results"][0]["result"], "SAT")
            self.assertEqual(result["phase_literals"], 6)
            self.assertEqual(result["actual_k"], 1)


if __name__ == "__main__":
    unittest.main()
