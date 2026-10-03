"""Independent, one-query SAT experiments for the FFP solver."""

import argparse
import csv
import json
import math
import multiprocessing
import os
import time
from pathlib import Path

from pysat.solvers import Solver

from .canonical import CanonicalActionEncoding
from .encoder import Encoder
from .guidance import PhaseMode, build_consensus_pool, build_phase_literals, validate_phase_literals
from .instance import read_instance
from .preprocess import preprocess
from .simulator import simulate


def positive_int(value):
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Expected an integer") from exc
    if number < 1:
        raise argparse.ArgumentTypeError("Value must be positive")
    return number


def nonnegative_int(value):
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Expected an integer") from exc
    if number < 0:
        raise argparse.ArgumentTypeError("Value must be non-negative")
    return number


def positive_float(value):
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Expected a number") from exc
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("Value must be finite and positive")
    return number


def load_result_schedule(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    schedule = data.get("schedule") if isinstance(data, dict) else data
    if not isinstance(schedule, list) or any(not isinstance(row, list) for row in schedule):
        raise ValueError("Schedule JSON must contain a list of action lists")
    return schedule, data if isinstance(data, dict) else {}


def _query_worker(connection, path, firefighters, solver_name, horizon, bound, fixed_rounds, started):
    solver = encoder = None
    metrics = {
        "worker_pid": os.getpid(),
        "T": horizon,
        "K": bound,
        "saved_required": None,
        "encoding_time": 0.0,
        "solve_time": 0.0,
        "variables": 0,
        "clauses": 0,
        "semantic_variables": 0,
        "auxiliary_variables": 0,
        "assumption_count": 0,
    }
    try:
        instance = read_instance(path)
        distance, _ = preprocess(instance, firefighters)
        solver = Solver(name=solver_name)
        encode_started = time.monotonic()
        encoder = Encoder(instance, firefighters, solver, distance)
        encoder.ensure_horizon(horizon)
        assumptions = encoder.assumptions(horizon, bound, instance.n)
        fixed_actions = []
        for round_number, selected in fixed_rounds:
            selected = set(selected)
            if len(selected) > firefighters:
                raise ValueError(f"Round {round_number} fixes more actions than firefighters available")
            for vertex in range(instance.n):
                literal = encoder.a[vertex, round_number]
                if vertex in selected:
                    assumptions.append(literal)
                    fixed_actions.append([round_number, vertex])
                elif len(selected) < firefighters:
                    # Pin unused firefighters too, so a prefix round is exact.
                    assumptions.append(-literal)
        metrics["encoding_time"] = time.monotonic() - encode_started
        metrics.update(
            saved_required=instance.n - bound,
            variables=encoder.vars.top,
            clauses=encoder.clauses,
            containment_clauses=2 * instance.m,
            containment_activation_variables=1,
            semantic_variables=encoder.vars.semantic,
            auxiliary_variables=encoder.vars.auxiliary,
            assumption_count=len(assumptions),
            fixed_actions=fixed_actions,
        )
        connection.send(("ENCODED", metrics))
        connection.send(("SOLVE_STARTED", time.monotonic() - started))
        solve_started = time.monotonic()
        satisfiable = solver.solve(assumptions=assumptions)
        metrics["solve_time"] = time.monotonic() - solve_started
        try:
            stats = solver.accum_stats()
        except (AttributeError, NotImplementedError):
            stats = {}
        for key in ("decisions", "conflicts", "propagations", "restarts"):
            if key in stats:
                metrics[key] = stats[key]
        if satisfiable:
            model = set(solver.get_model())
            schedule = [list(actions) for actions in encoder.decode(model, horizon)]
            validation_started = time.monotonic()
            solution = simulate(instance, firefighters, schedule)
            metrics["validation_time"] = time.monotonic() - validation_started
            if solution.k > bound or solution.containment_time > horizon:
                raise AssertionError("SAT model failed independent schedule validation")
            metrics.update(
                actual_k=solution.k,
                containment_time=solution.containment_time,
                schedule=[list(actions) for actions in solution.schedule],
            )
        metrics["result"] = "SAT" if satisfiable else "UNSAT"
        connection.send(("RESULT", metrics))
    except Exception as exc:
        metrics.update(result="ERROR", error=f"{type(exc).__name__}: {exc}")
        connection.send(("RESULT", metrics))
    finally:
        if encoder is not None:
            encoder.close()
        if solver is not None:
            solver.delete()
        connection.close()


def _phase_worker(
    connection,
    path,
    firefighters,
    solver_name,
    horizon,
    bound,
    mode,
    incumbent_schedule,
    supplied_schedules,
    pool_size,
    seed,
    replay_bounds,
    started,
):
    solver = encoder = None
    metrics = {
        "worker_pid": os.getpid(),
        "experiment": "phase_guidance_replay" if replay_bounds else "phase_guidance",
        "phase_mode": mode,
        "T": horizon,
        "K": bound,
        "result": "ERROR",
        "encoding_time": 0.0,
        "guidance_build_time": 0.0,
        "solve_time": 0.0,
        "replay_results": [],
        "phase_literals": 0,
        "phase_positive": 0,
        "phase_negative": 0,
        "variables": 0,
        "clauses": 0,
        "semantic_variables": 0,
        "auxiliary_variables": 0,
        "assumption_count": 0,
    }
    try:
        instance = read_instance(path)
        distance, _ = preprocess(instance, firefighters)
        solver = Solver(name=solver_name)
        if not hasattr(solver, "set_phases"):
            raise RuntimeError(f"Solver backend {solver_name!r} does not support set_phases()")

        encoding_time = 0.0
        encoder = Encoder(instance, firefighters, solver, distance)
        encode_started = time.monotonic()
        encoder.ensure_horizon(horizon)
        encoding_time += time.monotonic() - encode_started
        replay_results = []
        for replay_bound in replay_bounds:
            encode_started = time.monotonic()
            replay_assumptions = encoder.assumptions(horizon, replay_bound, instance.n)
            encoding_time += time.monotonic() - encode_started
            before = solver.accum_stats() if hasattr(solver, "accum_stats") else {}
            replay_started = time.monotonic()
            replay_sat = solver.solve(assumptions=replay_assumptions)
            replay_time = time.monotonic() - replay_started
            after = solver.accum_stats() if hasattr(solver, "accum_stats") else {}
            replay_results.append(
                {
                    "K": replay_bound,
                    "result": "SAT" if replay_sat else "UNSAT",
                    "solve_time": replay_time,
                    "decisions": after.get("decisions", 0) - before.get("decisions", 0),
                    "conflicts": after.get("conflicts", 0) - before.get("conflicts", 0),
                    "propagations": after.get("propagations", 0) - before.get("propagations", 0),
                    "restarts": after.get("restarts", 0) - before.get("restarts", 0),
                }
            )

        encode_started = time.monotonic()
        assumptions = encoder.assumptions(horizon, bound, instance.n)
        encoding_time += time.monotonic() - encode_started

        guidance_started = time.monotonic()
        pool = []
        pool_stats = None
        if mode == PhaseMode.CONSENSUS.value:
            pool, pool_stats = build_consensus_pool(
                instance,
                firefighters,
                supplied_schedules,
                pool_size,
                seed,
            )
            schedules = [solution.schedule for solution in pool]
        else:
            schedules = None
        phases, votes = build_phase_literals(
            encoder,
            instance,
            firefighters,
            horizon,
            mode,
            incumbent_schedule=incumbent_schedule,
            consensus_schedules=schedules,
        )
        phase_counts = validate_phase_literals(phases)
        if mode != PhaseMode.NONE.value:
            solver.set_phases(phases)
        metrics["guidance_build_time"] = time.monotonic() - guidance_started
        metrics.update(
            **phase_counts,
            consensus_pool=pool_stats,
            consensus_votes=votes,
            replay_results=replay_results,
            encoding_time=encoding_time,
            variables=encoder.vars.top,
            clauses=encoder.clauses,
            semantic_variables=encoder.vars.semantic,
            auxiliary_variables=encoder.vars.auxiliary,
            containment_clauses=encoder.debug_profile()["containment"]["number_of_clauses"]
            if encoder.debug
            else 2 * instance.m,
            containment_activation_variables=len(encoder.h),
            assumption_count=len(assumptions),
            assumptions=assumptions,
        )
        connection.send(("ENCODED", metrics))
        connection.send(("SOLVE_STARTED", time.monotonic() - started))
        before = solver.accum_stats() if hasattr(solver, "accum_stats") else {}
        solve_started = time.monotonic()
        satisfiable = solver.solve(assumptions=assumptions)
        metrics["solve_time"] = time.monotonic() - solve_started
        after = solver.accum_stats() if hasattr(solver, "accum_stats") else {}
        for key in ("decisions", "conflicts", "propagations", "restarts"):
            if key in after:
                metrics[key] = after[key] - before.get(key, 0)
        metrics["decisions_per_second"] = (
            metrics["decisions"] / metrics["solve_time"]
            if metrics.get("decisions") is not None and metrics["solve_time"] > 0
            else None
        )
        metrics["conflicts_per_decision"] = (
            metrics["conflicts"] / metrics["decisions"]
            if metrics.get("conflicts") is not None and metrics.get("decisions", 0) > 0
            else None
        )
        metrics["propagations_per_decision"] = (
            metrics["propagations"] / metrics["decisions"]
            if metrics.get("propagations") is not None and metrics.get("decisions", 0) > 0
            else None
        )

        if satisfiable:
            model = set(solver.get_model())
            schedule = [list(actions) for actions in encoder.decode(model, horizon)]
            solution = simulate(instance, firefighters, schedule)
            if solution.k > bound or solution.containment_time > horizon:
                raise AssertionError("SAT model failed independent schedule validation")
            metrics.update(
                actual_k=solution.k,
                containment_time=solution.containment_time,
                schedule=[list(actions) for actions in solution.schedule],
            )
        metrics["result"] = "SAT" if satisfiable else "UNSAT"
        connection.send(("RESULT", metrics))
    except Exception as exc:
        metrics.update(result="ERROR", error=f"{type(exc).__name__}: {exc}")
        connection.send(("RESULT", metrics))
    finally:
        if encoder is not None:
            encoder.close()
        if solver is not None:
            solver.delete()
        connection.close()


def run_case(path, firefighters, solver_name, horizon, bound, fixed_rounds, timeout):
    """Run one query with a fresh process and SAT solver."""
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    started = time.monotonic()
    process = context.Process(
        target=_query_worker,
        args=(sender, str(path), firefighters, solver_name, horizon, bound, fixed_rounds, started),
    )
    latest = {}
    solve_started = None
    final_received = False
    process.start()
    sender.close()
    try:
        while process.is_alive() and time.monotonic() - started < timeout:
            remaining = timeout - (time.monotonic() - started)
            if not receiver.poll(min(0.05, max(0.0, remaining))):
                continue
            try:
                kind, payload = receiver.recv()
            except (EOFError, OSError):
                break
            if kind == "ENCODED":
                latest = payload
            elif kind == "RESULT":
                latest = payload
                final_received = True
            elif kind == "SOLVE_STARTED":
                solve_started = started + payload
        timed_out = process.is_alive()
        if timed_out:
            process.terminate()
            process.join(0.2)
            if process.is_alive():
                process.kill()
                process.join(0.2)
        else:
            process.join(0.2)
        while receiver.poll():
            try:
                kind, payload = receiver.recv()
            except (EOFError, OSError):
                break
            if kind == "ENCODED":
                latest = payload
            elif kind == "RESULT":
                latest = payload
                final_received = True
            elif kind == "SOLVE_STARTED":
                solve_started = started + payload
        if timed_out and not final_received:
            latest["result"] = "TIMEOUT"
            latest["solve_time"] = (
                min(timeout, max(0.0, time.monotonic() - solve_started)) if solve_started else 0.0
            )
        elif not final_received:
            latest.update(result="ERROR", error=f"Worker exited with code {process.exitcode}")
        latest["wall_time"] = time.monotonic() - started
        defaults = {
            "T": horizon,
            "K": bound,
            "saved_required": None,
            "encoding_time": 0.0,
            "solve_time": 0.0,
            "variables": 0,
            "clauses": 0,
            "semantic_variables": 0,
            "auxiliary_variables": 0,
            "assumption_count": 0,
        }
        for key, value in defaults.items():
            latest.setdefault(key, value)
        return latest
    finally:
        if process.is_alive():
            process.kill()
            process.join(0.2)
        receiver.close()
        process.close()


def run_phase_case(
    path,
    firefighters,
    solver_name,
    horizon,
    bound,
    mode,
    incumbent_schedule,
    supplied_schedules,
    pool_size,
    seed,
    timeout,
    replay_bounds=(),
):
    """Run one phase-guidance query, optionally replaying earlier bounds first."""
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    started = time.monotonic()
    process = context.Process(
        target=_phase_worker,
        args=(
            sender,
            str(path),
            firefighters,
            solver_name,
            horizon,
            bound,
            mode,
            incumbent_schedule,
            supplied_schedules,
            pool_size,
            seed,
            tuple(replay_bounds),
            started,
        ),
    )
    latest = {}
    solve_started = None
    final_received = False
    process.start()
    sender.close()
    try:
        while process.is_alive() and time.monotonic() - started < timeout:
            remaining = timeout - (time.monotonic() - started)
            if not receiver.poll(min(0.05, max(0.0, remaining))):
                continue
            try:
                kind, payload = receiver.recv()
            except (EOFError, OSError):
                break
            if kind == "ENCODED":
                latest = payload
            elif kind == "RESULT":
                latest = payload
                final_received = True
            elif kind == "SOLVE_STARTED":
                solve_started = started + payload
        timed_out = process.is_alive()
        if timed_out:
            process.terminate()
            process.join(0.2)
            if process.is_alive():
                process.kill()
                process.join(0.2)
        else:
            process.join(0.2)
        while receiver.poll():
            try:
                kind, payload = receiver.recv()
            except (EOFError, OSError):
                break
            if kind == "ENCODED":
                latest = payload
            elif kind == "RESULT":
                latest = payload
                final_received = True
            elif kind == "SOLVE_STARTED":
                solve_started = started + payload
        if timed_out and not final_received:
            latest["result"] = "TIMEOUT"
            latest["solve_time"] = min(timeout, max(0.0, time.monotonic() - solve_started)) if solve_started else 0.0
        elif not final_received:
            latest.update(result="ERROR", error=f"Worker exited with code {process.exitcode}")
        latest["wall_time"] = time.monotonic() - started
        latest.setdefault("phase_mode", mode)
        latest.setdefault("experiment", "phase_guidance_replay" if replay_bounds else "phase_guidance")
        latest.setdefault("T", horizon)
        latest.setdefault("K", bound)
        latest.setdefault("replay_results", [])
        return latest
    finally:
        if process.is_alive():
            process.kill()
            process.join(0.2)
        receiver.close()
        process.close()


def phase_guidance_experiment(
    path,
    firefighters,
    solver,
    horizon,
    bound,
    modes,
    incumbent_schedule,
    supplied_schedules,
    pool_size,
    seed,
    timeout,
    replay_bounds=(),
):
    cases = []
    for mode in modes:
        case = run_phase_case(
            path,
            firefighters,
            solver,
            horizon,
            bound,
            mode,
            incumbent_schedule,
            supplied_schedules,
            pool_size,
            seed,
            timeout,
            replay_bounds,
        )
        cases.append(case)
        print(
            f"phase mode={mode}: {case['result']} {case.get('solve_time', 0.0):.2f}s "
            f"phases={case.get('phase_literals', 0)}",
            flush=True,
        )
    signatures = {
        (
            case.get("variables"),
            case.get("clauses"),
            case.get("assumption_count"),
            tuple(case.get("assumptions", ())),
        )
        for case in cases
        if case.get("variables") is not None
    }
    if len(signatures) > 1:
        raise AssertionError(f"Phase modes changed CNF or assumptions: {signatures}")
    return cases


def _canonical_worker(connection, path, firefighters, solver_name, horizon, bound, mode, started):
    solver = encoder = None
    metrics = {
        "worker_pid": os.getpid(),
        "experiment": "canonical_actions",
        "mode": mode,
        "T": horizon,
        "K": bound,
        "result": "ERROR",
        "encoding_time": 0.0,
        "canonical_encoding_time": 0.0,
        "solve_time": 0.0,
        "base_variables": 0,
        "base_clauses": 0,
        "base_semantic_variables": 0,
        "base_auxiliary_variables": 0,
        "active_variables": 0,
        "threat_witness_variables": 0,
        "active_state_clauses": 0,
        "stop_clauses": 0,
        "nonempty_clauses": 0,
        "full_capacity_clauses": 0,
        "full_capacity_auxiliary_variables": 0,
        "total_variables": 0,
        "total_clauses": 0,
        "assumption_count": 0,
    }
    try:
        instance = read_instance(path)
        distance, _ = preprocess(instance, firefighters)
        solver = Solver(name=solver_name)
        encode_started = time.monotonic()
        encoder = Encoder(instance, firefighters, solver, distance)
        encoder.ensure_horizon(horizon)
        assumptions = encoder.assumptions(horizon, bound, instance.n)
        metrics["encoding_time"] = time.monotonic() - encode_started
        metrics.update(
            base_variables=encoder.vars.top,
            base_clauses=encoder.clauses,
            base_semantic_variables=encoder.vars.semantic,
            base_auxiliary_variables=encoder.vars.auxiliary,
            assumption_count=len(assumptions),
            assumptions=assumptions,
        )

        canonical = CanonicalActionEncoding(encoder)
        canonical_started = time.monotonic()
        canonical.apply_mode(mode, horizon, firefighters)
        metrics["canonical_encoding_time"] = time.monotonic() - canonical_started
        canonical_stats = canonical.stats()
        metrics.update(
            **canonical_stats,
            total_variables=encoder.vars.top,
            total_clauses=encoder.clauses + sum(canonical_stats[key] for key in (
                "active_state_clauses",
                "stop_clauses",
                "nonempty_clauses",
                "full_capacity_clauses",
            )),
        )
        connection.send(("ENCODED", metrics))
        connection.send(("SOLVE_STARTED", time.monotonic() - started))
        solve_started = time.monotonic()
        satisfiable = solver.solve(assumptions=assumptions)
        metrics["solve_time"] = time.monotonic() - solve_started
        try:
            stats = solver.accum_stats()
        except (AttributeError, NotImplementedError):
            stats = {}
        for key in ("decisions", "conflicts", "propagations", "restarts"):
            if key in stats:
                metrics[key] = stats[key]
        if satisfiable:
            model = set(solver.get_model())
            schedule = [list(actions) for actions in encoder.decode(model, horizon)]
            solution = simulate(instance, firefighters, schedule)
            if solution.k > bound or solution.containment_time > horizon:
                raise AssertionError("SAT model failed independent schedule validation")
            metrics.update(
                actual_k=solution.k,
                containment_time=solution.containment_time,
                schedule=[list(actions) for actions in solution.schedule],
            )
        metrics["result"] = "SAT" if satisfiable else "UNSAT"
        connection.send(("RESULT", metrics))
    except Exception as exc:
        metrics.update(result="ERROR", error=f"{type(exc).__name__}: {exc}")
        connection.send(("RESULT", metrics))
    finally:
        if encoder is not None:
            encoder.close()
        if solver is not None:
            solver.delete()
        connection.close()


def run_canonical_case(path, firefighters, solver_name, horizon, bound, mode, timeout):
    """Run one canonical-action ablation in a fresh spawned solver process."""
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    started = time.monotonic()
    process = context.Process(
        target=_canonical_worker,
        args=(sender, str(path), firefighters, solver_name, horizon, bound, mode, started),
    )
    latest = {}
    solve_started = None
    final_received = False
    process.start()
    sender.close()
    try:
        while process.is_alive() and time.monotonic() - started < timeout:
            remaining = timeout - (time.monotonic() - started)
            if not receiver.poll(min(0.05, max(0.0, remaining))):
                continue
            try:
                kind, payload = receiver.recv()
            except (EOFError, OSError):
                break
            if kind == "ENCODED":
                latest = payload
            elif kind == "RESULT":
                latest = payload
                final_received = True
            elif kind == "SOLVE_STARTED":
                solve_started = started + payload
        timed_out = process.is_alive()
        if timed_out:
            process.terminate()
            process.join(0.2)
            if process.is_alive():
                process.kill()
                process.join(0.2)
        else:
            process.join(0.2)
        while receiver.poll():
            try:
                kind, payload = receiver.recv()
            except (EOFError, OSError):
                break
            if kind == "ENCODED":
                latest = payload
            elif kind == "RESULT":
                latest = payload
                final_received = True
            elif kind == "SOLVE_STARTED":
                solve_started = started + payload
        if timed_out and not final_received:
            latest["result"] = "TIMEOUT"
            latest["solve_time"] = min(timeout, max(0.0, time.monotonic() - solve_started)) if solve_started else 0.0
        elif not final_received:
            latest.update(result="ERROR", error=f"Worker exited with code {process.exitcode}")
        latest["wall_time"] = time.monotonic() - started
        latest.setdefault("mode", mode)
        latest.setdefault("experiment", "canonical_actions")
        latest.setdefault("T", horizon)
        latest.setdefault("K", bound)
        return latest
    finally:
        if process.is_alive():
            process.kill()
            process.join(0.2)
        receiver.close()
        process.close()


def canonical_actions_experiment(path, firefighters, solver, horizon, bound, modes, timeout):
    cases = []
    for mode in modes:
        case = run_canonical_case(path, firefighters, solver, horizon, bound, mode, timeout)
        cases.append(case)
        print(
            f"canonical mode={mode}: {case['result']} {case.get('solve_time', 0.0):.2f}s "
            f"vars={case.get('total_variables', 0)} clauses={case.get('total_clauses', 0)}",
            flush=True,
        )
    signatures = {
        (
            case.get("base_variables"),
            case.get("base_clauses"),
            tuple(case.get("assumptions", ())),
        )
        for case in cases
        if case.get("base_variables") is not None
    }
    if len(signatures) > 1:
        raise AssertionError(f"Canonical modes changed the base formula or query: {signatures}")
    return cases


def fixed_prefix_experiment(path, firefighters, solver, horizon, bound, schedule, max_prefix, timeout):
    cases = []
    for prefix_length in range(max_prefix + 1):
        fixed = [(round_number, schedule[round_number - 1]) for round_number in range(1, prefix_length + 1)]
        case = run_case(path, firefighters, solver, horizon, bound, fixed, timeout)
        case.update(
            experiment="fixed_prefix",
            prefix_length=prefix_length,
            fixed_actions=[[t, v] for t, selected in fixed for v in selected],
        )
        cases.append(case)
        print(f"fixed_prefix r={prefix_length}: {case['result']} {case['wall_time']:.2f}s", flush=True)
    return cases


def horizon_heatmap(path, firefighters, solver, bound, horizons, timeout):
    cases = []
    for horizon in horizons:
        case = run_case(path, firefighters, solver, horizon, bound, [], timeout)
        case["experiment"] = "horizon_heatmap"
        cases.append(case)
        print(f"horizon T={horizon}: {case['result']} {case['wall_time']:.2f}s", flush=True)
    return cases


def _write_outputs(out_dir, stem, cases, metadata):
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"{stem}.json"
    payload = {**metadata, "cases": cases}
    json_path.write_text(json.dumps(payload, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    fields = (
        "experiment",
        "worker_pid",
        "prefix_length",
        "T",
        "K",
        "saved_required",
        "result",
        "solve_time",
        "encoding_time",
        "variables",
        "clauses",
        "containment_clauses",
        "containment_activation_variables",
        "semantic_variables",
        "auxiliary_variables",
        "assumption_count",
        "decisions",
        "conflicts",
        "propagations",
        "restarts",
        "actual_k",
        "containment_time",
        "schedule",
        "fixed_actions",
        "phase_mode",
        "phase_literals",
        "phase_positive",
        "phase_negative",
        "guidance_build_time",
        "replay_results",
        "consensus_pool",
        "consensus_votes",
        "assumptions",
        "decisions_per_second",
        "conflicts_per_decision",
        "propagations_per_decision",
        "mode",
        "base_variables",
        "base_clauses",
        "base_semantic_variables",
        "base_auxiliary_variables",
        "active_variables",
        "threat_witness_variables",
        "active_state_clauses",
        "stop_clauses",
        "nonempty_clauses",
        "full_capacity_clauses",
        "full_capacity_auxiliary_variables",
        "total_variables",
        "total_clauses",
        "canonical_encoding_time",
        "wall_time",
        "error",
    )
    csv_path = out_dir / f"{stem}.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for case in cases:
            writer.writerow(
                {
                    **case,
                    "schedule": json.dumps(case.get("schedule")),
                    "fixed_actions": json.dumps(case.get("fixed_actions")),
                    "replay_results": json.dumps(case.get("replay_results")),
                    "consensus_pool": json.dumps(case.get("consensus_pool")),
                    "consensus_votes": json.dumps(case.get("consensus_votes")),
                    "assumptions": json.dumps(case.get("assumptions")),
                }
            )
    return json_path, csv_path


def main(argv=None):
    parser = argparse.ArgumentParser(description="Independent FFP SAT diagnostic experiments")
    subparsers = parser.add_subparsers(dest="experiment", required=True)
    fixed = subparsers.add_parser("fixed-prefix", help="Solve F(T,K) with increasing incumbent prefixes")
    fixed.add_argument("instance", type=Path)
    fixed.add_argument("--firefighters", type=positive_int, required=True)
    fixed.add_argument("--T", type=nonnegative_int, required=True)
    fixed.add_argument("--K", type=nonnegative_int, required=True)
    fixed.add_argument(
        "--schedule", type=Path, required=True, help="Result JSON containing an incumbent schedule"
    )
    fixed.add_argument("--max-prefix", type=nonnegative_int)
    heatmap = subparsers.add_parser("heatmap", help="Solve F(T,K) independently at each requested horizon")
    heatmap.add_argument("instance", type=Path)
    heatmap.add_argument("--firefighters", type=positive_int, required=True)
    heatmap.add_argument("--K", type=nonnegative_int, required=True)
    heatmap.add_argument("--horizons", type=nonnegative_int, nargs="+", required=True)
    phase = subparsers.add_parser(
        "phase-guidance", help="Compare CaDiCaL phase preferences on one F(T,K) query"
    )
    phase.add_argument("instance", type=Path)
    phase.add_argument("--firefighters", type=positive_int, required=True)
    phase.add_argument("--T", type=nonnegative_int, required=True)
    phase.add_argument("--K", type=nonnegative_int, required=True)
    phase.add_argument("--schedule", type=Path, required=True, help="JSON result containing an incumbent schedule")
    phase.add_argument(
        "--modes",
        choices=[mode.value for mode in PhaseMode],
        nargs="+",
        default=[mode.value for mode in PhaseMode],
    )
    phase.add_argument("--consensus-pool-size", type=positive_int, default=32)
    phase.add_argument("--seed", type=int, default=0)
    phase.add_argument(
        "--replay-bounds",
        type=nonnegative_int,
        nargs="*",
        default=[],
        help="Optional preceding K bounds solved normally on the same solver before applying guidance",
    )
    canonical = subparsers.add_parser(
        "canonical-actions", help="Ablate exact active-state and canonical action constraints"
    )
    canonical.add_argument("instance", type=Path)
    canonical.add_argument("--firefighters", type=positive_int, required=True)
    canonical.add_argument("--T", type=nonnegative_int, required=True)
    canonical.add_argument("--K", type=nonnegative_int, required=True)
    canonical.add_argument(
        "--modes",
        choices=["base", "active-only", "stop-after-contained", "canonical"],
        nargs="+",
        default=["base", "active-only", "stop-after-contained", "canonical"],
    )
    for command in (fixed, heatmap):
        command.add_argument("--per-query-time", type=positive_float, default=30.0)
        command.add_argument("--solver", default="cadical300")
        command.add_argument("--out-dir", type=Path)
    for command in (phase, canonical):
        command.add_argument("--per-query-time", type=positive_float, default=60.0)
        command.add_argument("--solver", default="cadical300")
        command.add_argument("--out-dir", type=Path)
    args = parser.parse_args(argv)

    try:
        instance = read_instance(args.instance)
    except (OSError, ValueError) as exc:
        parser.error(f"Cannot read instance: {exc}")
    metadata = {
        "instance": str(args.instance),
        "n": instance.n,
        "m": instance.m,
        "firefighters": args.firefighters,
        "solver": args.solver,
        "per_query_time": args.per_query_time,
    }
    if args.K > instance.n:
        parser.error("--K must not exceed the number of vertices")
    if args.experiment == "canonical-actions":
        if len(set(args.modes)) != len(args.modes):
            parser.error("--modes must not contain duplicates")
        metadata.update(T=args.T, K=args.K, modes=args.modes)
        cases = canonical_actions_experiment(
            args.instance,
            args.firefighters,
            args.solver,
            args.T,
            args.K,
            args.modes,
            args.per_query_time,
        )
        stem = "canonical_actions"
    elif args.experiment == "phase-guidance":
        if "action" in args.modes or "full" in args.modes:
            try:
                schedule, schedule_meta = load_result_schedule(args.schedule)
                incumbent = simulate(instance, args.firefighters, schedule)
            except (OSError, ValueError, TypeError) as exc:
                parser.error(f"Cannot validate incumbent schedule: {exc}")
            if "best_k" in schedule_meta and incumbent.k != schedule_meta["best_k"]:
                parser.error(
                    f"Input schedule validates to K={incumbent.k}, expected K={schedule_meta['best_k']}"
                )
            incumbent_schedule = incumbent.schedule
        else:
            try:
                raw_schedule, schedule_meta = load_result_schedule(args.schedule)
                incumbent_schedule = simulate(instance, args.firefighters, raw_schedule).schedule
            except (OSError, ValueError, TypeError) as exc:
                parser.error(f"Cannot validate incumbent schedule: {exc}")
        supplied_schedules = [incumbent_schedule]
        for row in schedule_meta.get("pareto_frontier", []):
            if isinstance(row, dict) and isinstance(row.get("schedule"), list):
                supplied_schedules.append(row["schedule"])
        try:
            probe = Solver(name=args.solver)
            if not hasattr(probe, "set_phases"):
                parser.error(f"SAT backend {args.solver!r} does not support set_phases(); phase experiment stopped")
            probe.delete()
        except Exception as exc:
            parser.error(f"Cannot initialize SAT backend {args.solver!r}: {exc}")
        if any(k <= args.K or k > instance.n for k in args.replay_bounds):
            parser.error("Every --replay-bounds value must be greater than --K and at most n")
        if any(left <= right for left, right in zip(args.replay_bounds, args.replay_bounds[1:])):
            parser.error("--replay-bounds must be strictly descending")
        metadata.update(
            T=args.T,
            K=args.K,
            schedule_input=str(args.schedule),
            incumbent_k=simulate(instance, args.firefighters, incumbent_schedule).k,
            modes=args.modes,
            consensus_pool_size=args.consensus_pool_size,
            seed=args.seed,
            replay_bounds=args.replay_bounds,
        )
        cases = phase_guidance_experiment(
            args.instance,
            args.firefighters,
            args.solver,
            args.T,
            args.K,
            args.modes,
            incumbent_schedule,
            supplied_schedules,
            args.consensus_pool_size,
            args.seed,
            args.per_query_time,
            args.replay_bounds,
        )
        stem = "phase_guidance_replay" if args.replay_bounds else "phase_guidance"
    elif args.experiment == "fixed-prefix":
        try:
            schedule, schedule_meta = load_result_schedule(args.schedule)
            verified = simulate(instance, args.firefighters, schedule)
        except (OSError, ValueError, TypeError) as exc:
            parser.error(f"Cannot validate incumbent schedule: {exc}")
        if "best_k" in schedule_meta and verified.k != schedule_meta["best_k"]:
            parser.error(f"Input schedule validates to K={verified.k}, expected K={schedule_meta['best_k']}")
        max_prefix = len(verified.schedule) if args.max_prefix is None else args.max_prefix
        if max_prefix > len(verified.schedule):
            parser.error("--max-prefix exceeds the validated schedule length")
        if max_prefix > args.T:
            parser.error("--max-prefix cannot exceed --T")
        metadata.update(
            T=args.T,
            K=args.K,
            incumbent_k=verified.k,
            incumbent_containment_time=verified.containment_time,
            schedule_input=str(args.schedule),
        )
        cases = fixed_prefix_experiment(
            args.instance,
            args.firefighters,
            args.solver,
            args.T,
            args.K,
            verified.schedule,
            max_prefix,
            args.per_query_time,
        )
        stem = "fixed_prefix"
    else:
        if len(set(args.horizons)) != len(args.horizons):
            parser.error("--horizons must not contain duplicates")
        metadata.update(K=args.K, horizons=args.horizons)
        cases = horizon_heatmap(
            args.instance,
            args.firefighters,
            args.solver,
            args.K,
            args.horizons,
            args.per_query_time,
        )
        stem = "horizon_heatmap"
    if args.out_dir is None:
        args.out_dir = Path("diagnostics")
    json_path, csv_path = _write_outputs(args.out_dir, stem, cases, metadata)
    print(f"JSON: {json_path}\nCSV: {csv_path}", flush=True)
    return int(any(case["result"] == "ERROR" for case in cases))


if __name__ == "__main__":
    raise SystemExit(main())
