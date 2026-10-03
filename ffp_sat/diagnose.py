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

from .encoder import Encoder
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
    for command in (fixed, heatmap):
        command.add_argument("--per-query-time", type=positive_float, default=30.0)
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
    if args.experiment == "fixed-prefix":
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
