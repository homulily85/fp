import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from pysat.solvers import Solver

from ffp_sat.simulator import simulate as trusted_simulate
from ffp_sat.worker import run, validate_final_result, worker


def hanging_worker(connection, path, d, config, started, deadline):
    if config.get("checkpoint"):
        connection.send(
            (
                "CHECKPOINT",
                dict(
                    status="FEASIBLE",
                    termination="TIME_LIMIT",
                    lower_bound=1,
                    upper_bound=2,
                    best_k=2,
                    schedule=[[2]],
                ),
            )
        )
    time.sleep(10)


def crashing_worker(connection, path, d, config, started, deadline):
    connection.close()
    raise SystemExit(7)


def stubborn_worker(connection, path, d, config, started, deadline):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    connection.send(("CHECKPOINT", dict(lower_bound=1, upper_bound=2, best_k=2, schedule=[[2]])))
    time.sleep(10)


def final_worker(connection, path, d, config, started, deadline):
    connection.send(("FINAL", dict(status="OPTIMAL", termination="PROVEN", lower_bound=1, upper_bound=1)))
    connection.close()


def schedule_worker(connection, path, d, config, started, deadline):
    connection.send(
        (
            "FINAL",
            dict(
                status="OPTIMAL",
                termination="PROVEN",
                n=2,
                m=1,
                firefighters=d,
                best_k=1,
                saved=1,
                lower_bound=1,
                upper_bound=1,
                schedule=[[1]],
                best_containment_time=1,
            ),
        )
    )
    connection.close()


class Collector:
    def __init__(self):
        self.messages = []

    def send(self, message):
        self.messages.append(message)

    def close(self):
        pass


class ProcessTests(unittest.TestCase):
    def test_timeout_checkpoint(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "tiny.in"
            path.write_text("0\n3\n2\nx\n1\n0\n0 1\n1 2\n")
            for checkpoint in (False, True):
                result = run(str(path), 1, dict(time_limit=0.4, checkpoint=checkpoint), hanging_worker)
                self.assertLess(result["elapsed_total"], 1.5)
                self.assertEqual(result["status"], "FEASIBLE" if checkpoint else "ERROR")
                self.assertEqual(
                    result["termination"], "TIME_LIMIT" if checkpoint else "TIME_LIMIT_NO_INCUMBENT"
                )
                if checkpoint:
                    self.assertEqual(result["schedule"], [[2]])
                    self.assertEqual(result["final_validation"], "PASSED")

    def test_kill_fallback_and_final(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "tiny.in"
            path.write_text("0\n3\n2\nx\n1\n0\n0 1\n1 2\n")
            result = run(str(path), 1, dict(time_limit=0.5), stubborn_worker)
            self.assertEqual(result["status"], "FEASIBLE")
            self.assertLess(result["elapsed_total"], 1.5)
            self.assertEqual(result["final_validation"], "PASSED")
        result = run("unused", 1, dict(time_limit=1), final_worker)
        self.assertEqual(result["termination"], "PROVEN")
        self.assertEqual(result["final_validation"], "SKIPPED_NO_SCHEDULE")

    def test_final_validation_runs_once_outside_solver_time(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "tiny.in"
            path.write_text("0\n2\n1\nx\n1\n0\n0 1\n")
            original = trusted_simulate

            def slow_check(*args, **kwargs):
                time.sleep(0.15)
                return original(*args, **kwargs)

            wall_started = time.monotonic()
            with patch("ffp_sat.worker.simulate", side_effect=slow_check) as checker:
                result = run(str(path), 1, dict(time_limit=1), schedule_worker)
            wall_elapsed = time.monotonic() - wall_started
            self.assertEqual(checker.call_count, 1)
            self.assertEqual(result["final_validation"], "PASSED")
            self.assertGreaterEqual(result["final_validation_time"], 0.14)
            self.assertGreater(wall_elapsed - result["elapsed_total"], 0.1)

    def test_final_validation_checks_horizon_and_preserves_worker_error(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "path.in"
            path.write_text("0\n3\n2\nx\n1\n0\n0 1\n1 2\n")
            too_short = dict(
                status="FEASIBLE",
                termination="TIME_LIMIT",
                best_k=3,
                upper_bound=3,
                lower_bound=1,
                schedule=[[], []],
                incumbent_horizon=1,
            )
            checked = validate_final_result(path, 1, too_short)
            self.assertEqual(checked["status"], "ERROR")
            self.assertEqual(checked["reason"], "MODEL_VALIDATION_FAILED")
            self.assertEqual(checked["final_validation"], "FAILED")

            worker_error = dict(
                status="ERROR",
                termination="WORKER_ERROR",
                best_k=1,
                upper_bound=1,
                lower_bound=1,
                schedule=[[1]],
                incumbent_horizon=1,
            )
            valid = validate_final_result(path, 1, worker_error)
            self.assertEqual(valid["final_validation"], "PASSED")
            self.assertEqual(valid["status"], "ERROR")
            self.assertEqual(valid["termination"], "WORKER_ERROR")

    def test_worker_uses_one_solver(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "instance.in"
            path.write_text("0\n6\n7\nx\n1\n0\n0 1\n0 5\n1 3\n1 4\n2 4\n2 5\n4 5\n")
            collector = Collector()
            started = time.monotonic()
            with patch("ffp_sat.worker.Solver", wraps=Solver) as constructor:
                with patch("ffp_sat.stk_solver.Solver", side_effect=AssertionError("Solver rebuilt")):
                    worker(
                        collector,
                        str(path),
                        1,
                        dict(solver="cadical300", heuristic_budget=0.001, seed=0, time_limit=2),
                        started,
                        started + 2,
                    )
            self.assertEqual(constructor.call_count, 1)
            self.assertEqual(collector.messages[-1][0], "FINAL")
            self.assertEqual(collector.messages[-1][1]["status"], "OPTIMAL")

    def test_crash(self):
        result = run("unused", 1, dict(time_limit=2), crashing_worker)
        self.assertEqual(result["termination"], "WORKER_EXIT")

    def test_backend_and_cli_errors(self):
        result = run(
            "dataset/50_ep0.1_0_gilbert_1.in", 1,
            dict(time_limit=2, solver="missing-backend", heuristic_budget=0.001, seed=0),
        )
        self.assertEqual(result["status"], "ERROR")
        self.assertEqual(result["termination"], "WORKER_ERROR")
        self.assertEqual(result["final_validation"], "PASSED")
        self.assertIn("missing-backend", result["error"])
        for extra in (
            ["--firefighters", "0"], ["--firefighters", "1", "--time-limit", "nan"],
            ["--firefighters", "1", "--initial-horizon-factor", "0.5"],
            ["--firefighters", "1", "--horizon-growth-factor", "inf"],
        ):
            process = subprocess.run(
                [sys.executable, "-m", "ffp_sat", "unused", *extra], capture_output=True, timeout=5
            )
            self.assertEqual(process.returncode, 2)

    def test_logs_update_source_and_sat_outcome(self):
        with tempfile.TemporaryDirectory() as root:
            instance = Path(root) / "sat_case.in"
            instance.write_text("0\n6\n7\nx\n1\n0\n0 1\n0 5\n1 3\n1 4\n2 4\n2 5\n4 5\n")
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "ffp_sat",
                    str(instance),
                    "--firefighters",
                    "1",
                    "--time-limit",
                    "3",
                    "--heuristic-budget",
                    "0.001",
                ],
                capture_output=True,
                text=True,
                timeout=6,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("[HEURISTIC]", result.stderr)
            self.assertIn("[PREPROCESS]", result.stderr)
            self.assertIn("[SAT_QUERY]", result.stderr)
            self.assertIn("[UNSAT]", result.stderr)
            self.assertIn("[HORIZON_BOUND]", result.stderr)
            self.assertIn("query_T=", result.stderr)
            self.assertIn("certifying=true", result.stderr)
            self.assertNotIn("old=", result.stderr)
            self.assertNotIn("current_T=", result.stderr)
            self.assertNotIn("max_encoded_T=", result.stderr)
            self.assertNotIn("starting worker", result.stderr)

    def test_cli_and_batch(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            output = root / "result.json"
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "ffp_sat",
                    "dataset/50_ep0.1_0_gilbert_1.in",
                    "--firefighters",
                    "1",
                    "--time-limit",
                    "1",
                    "--json-out",
                    str(output),
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            parsed = json.loads(output.read_text())
            self.assertIn(parsed["status"], ("OPTIMAL", "FEASIBLE"))
            self.assertEqual(parsed["final_validation"], "PASSED")
            self.assertEqual(parsed["initial_horizon_factor"], 1.5)
            self.assertEqual(parsed["horizon_growth_factor"], 2.0)
            self.assertEqual(parsed["t_max"], parsed["t_old_safe"])
            self.assertLessEqual(parsed["max_encoded_t"], parsed["t_struct"])
            self.assertLess(parsed["elapsed_total"], 2)
            dataset = root / "data"
            dataset.mkdir()
            (dataset / "tiny.in").write_text("0\n2\n1\nx\n1\n0\n0 1\n")
            (dataset / "bad.in").write_text("bad")
            command = [
                sys.executable,
                "-m",
                "ffp_sat.batch",
                str(dataset),
                "--firefighters",
                "1",
                "2",
                "--time-limit",
                "1",
                "--out-dir",
                str(root / "out"),
            ]
            batch = subprocess.run(command, capture_output=True, text=True, timeout=10)
            self.assertEqual(batch.returncode, 1, batch.stderr)
            self.assertEqual(len(list((root / "out").glob("*.json"))), 4)
            self.assertEqual(len((root / "out" / "summary.csv").read_text().splitlines()), 5)
            self.assertEqual(subprocess.run(command, capture_output=True).returncode, 2)

            single_command = [
                sys.executable,
                "-m",
                "ffp_sat.batch",
                str(dataset / "tiny.in"),
                "--firefighters",
                "[1,2]",
                "--time-limit",
                "1",
                "--out-dir",
                str(root / "single-out"),
            ]
            single = subprocess.run(single_command, capture_output=True, text=True, timeout=10)
            self.assertEqual(single.returncode, 0, single.stderr)
            self.assertTrue((root / "single-out" / "tiny_D1_seed0.json").exists())
            self.assertTrue((root / "single-out" / "tiny_D2_seed0.json").exists())

            default_command = [
                sys.executable,
                "-m",
                "ffp_sat.batch",
                str(dataset / "tiny.in"),
                "--firefighters",
                "1",
                "--time-limit",
                "1",
            ]
            env = dict(os.environ)
            repo_root = str(Path(__file__).resolve().parents[1])
            env["PYTHONPATH"] = repo_root + os.pathsep + env.get("PYTHONPATH", "")
            default_output = subprocess.run(
                default_command, cwd=root, env=env, capture_output=True, text=True, timeout=10
            )
            self.assertEqual(default_output.returncode, 0, default_output.stderr)
            result_dirs = list((root / "results").glob("tiny_sat-ffp_*"))
            self.assertEqual(len(result_dirs), 1)
            datetime.strptime(result_dirs[0].name.removeprefix("tiny_sat-ffp_"), "%Y-%m-%d-%H-%M-%S")
            self.assertTrue((result_dirs[0] / "summary.csv").exists())

            clean_dataset = root / "my-dataset"
            clean_dataset.mkdir()
            (clean_dataset / "tiny.in").write_text("0\n2\n1\nx\n1\n0\n0 1\n")
            folder_command = [
                sys.executable,
                "-m",
                "ffp_sat.batch",
                str(clean_dataset),
                "--firefighters",
                "[1,2]",
                "--time-limit",
                "1",
            ]
            folder_output = subprocess.run(
                folder_command, cwd=root, env=env, capture_output=True, text=True, timeout=10
            )
            self.assertEqual(folder_output.returncode, 0, folder_output.stderr)
            folder_dirs = list((root / "results").glob("my-dataset_sat-ffp_*"))
            self.assertEqual(len(folder_dirs), 1)
            datetime.strptime(folder_dirs[0].name.removeprefix("my-dataset_sat-ffp_"), "%Y-%m-%d-%H-%M-%S")
            self.assertTrue((folder_dirs[0] / "tiny_D1_seed0.json").exists())
            self.assertTrue((folder_dirs[0] / "tiny_D2_seed0.json").exists())

            invalid_range = subprocess.run(
                [*single_command[:5], "[3,1]", *single_command[6:]], capture_output=True, timeout=5
            )
            self.assertEqual(invalid_range.returncode, 2)
