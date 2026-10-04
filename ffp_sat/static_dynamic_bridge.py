"""Bridge static separator witnesses to exact temporal FFP queries."""

from __future__ import annotations

import csv
import json
import multiprocessing
import random
import time
from itertools import combinations
from pathlib import Path

from pysat.solvers import Solver

from .encoder import Encoder
from .instance import Instance, read_instance
from .preprocess import preprocess
from .simulator import simulate
from .static_separator import run_static_separator, validate_static_witness


def _add_pair_state_requirements(encoder, solver, pair, horizon):
    for vertex in pair:
        solver.add_clause([-encoder.b[vertex, horizon]])
        solver.add_clause([-encoder.d[vertex, horizon]])


def _solve_temporal_query(
    instance,
    firefighters,
    horizon,
    solver_name,
    mode,
    pair,
    separator=None,
    burned_bound=None,
):
    distance, _ = preprocess(instance, firefighters)
    with Solver(name=solver_name) as solver:
        encoder = Encoder(instance, firefighters, solver, distance)
        try:
            started = time.monotonic()
            encoder.ensure_horizon(horizon)
            containment = encoder.ensure_containment(horizon)
            assumptions = [containment]
            if mode == "exact-separator":
                assumptions.extend(encoder.d[vertex, horizon] for vertex in separator)
                _add_pair_state_requirements(encoder, solver, pair, horizon)
            elif mode == "pair-only":
                _add_pair_state_requirements(encoder, solver, pair, horizon)
                for round_number in range(1, horizon + 1):
                    solver.add_clause([encoder.a[vertex, round_number] for vertex in range(instance.n)])
            elif mode == "pair-base-regression":
                if burned_bound is None:
                    raise ValueError("The base pair regression requires a burned bound")
                assumptions.extend(encoder.assumptions(horizon, burned_bound, instance.n)[1:])
                _add_pair_state_requirements(encoder, solver, pair, horizon)
            elif mode == "pair-exact-regression":
                if burned_bound is None:
                    raise ValueError("The exact pair regression requires a burned bound")
                assumptions.extend(encoder.assumptions(horizon, burned_bound, instance.n)[1:])
                _add_pair_state_requirements(encoder, solver, pair, horizon)
                for round_number in range(1, horizon + 1):
                    solver.add_clause([encoder.a[vertex, round_number] for vertex in range(instance.n)])
            else:
                raise ValueError(f"Unknown temporal query mode: {mode}")

            encoding_time = time.monotonic() - started
            before = solver.accum_stats()
            solve_started = time.monotonic()
            satisfiable = solver.solve(assumptions=assumptions)
            solve_time = time.monotonic() - solve_started
            after = solver.accum_stats()
            stats = {
                key: after.get(key, 0) - before.get(key, 0)
                for key in ("decisions", "conflicts", "propagations", "restarts")
            }
            output = {
                "mode": mode,
                "result": "SAT" if satisfiable else "UNSAT",
                "encoding_time": encoding_time,
                "solve_time": solve_time,
                "variables": encoder.vars.top,
                "semantic_variables": encoder.vars.semantic,
                "auxiliary_variables": encoder.vars.auxiliary,
                "clauses": encoder.clauses
                + 2 * len(pair)
                + (horizon if mode in {"pair-only", "pair-exact-regression"} else 0),
                "pair_state_unit_clauses": 2 * len(pair),
                "pair_exact_action_clauses": horizon if mode in {"pair-only", "pair-exact-regression"} else 0,
                "objective_aux_variables": sum(
                    objective.saved_counter.auxiliary_variables if objective.saved_counter is not None else 0
                    for objective in encoder.objectives.values()
                ),
                "objective_clauses": sum(
                    objective.saved_counter.number_of_clauses if objective.saved_counter is not None else 0
                    for objective in encoder.objectives.values()
                ),
                "assumptions": assumptions,
                "stats": stats,
            }
            if satisfiable:
                model = set(solver.get_model())
                schedule = [list(actions) for actions in encoder.decode(model, horizon)]
                solution = simulate(instance, firefighters, schedule)
                defended_at_horizon = {
                    vertex
                    for vertex in range(instance.n)
                    if encoder.d[vertex, horizon] in model
                }
                burned_at_horizon = {
                    vertex
                    for vertex in range(instance.n)
                    if encoder.b[vertex, horizon] in model
                }
                if pair & (set(solution.burned) | set(solution.defended)):
                    raise AssertionError("Requested untouched pair is not untouched in decoded schedule")
                if pair & (burned_at_horizon | defended_at_horizon):
                    raise AssertionError("Requested untouched pair is not untouched in SAT state")
                if mode == "exact-separator" and defended_at_horizon != set(separator):
                    raise AssertionError("Decoded defended set differs from the fixed separator")
                if mode == "pair-only" and solution.k > instance.n - (horizon + 2):
                    raise AssertionError("Pair-only model does not meet the saved target")
                if mode in {"pair-only", "pair-exact-regression"} and any(
                    len(actions) != 1 for actions in schedule
                ):
                    raise AssertionError("Pair-exact model must use exactly one action in every round")
                output.update(
                    actual_k=solution.k,
                    saved=instance.n - solution.k,
                    containment_time=solution.containment_time,
                    schedule=[list(actions) for actions in solution.schedule],
                    defended=sorted(defended_at_horizon),
                    burned=sorted(burned_at_horizon),
                    validated=True,
                )
            return output
        finally:
            encoder.close()


def _small_graph_pair_regression(seed=0):
    """Exhaustively compare base and exactly-one-action pair queries on small graphs."""
    rng = random.Random(seed)
    instances = []
    for n in range(2, 5):
        edges = list(combinations(range(n), 2))
        for edge_mask in range(1 << len(edges)):
            adjacency = [set() for _ in range(n)]
            for index, (u, v) in enumerate(edges):
                if edge_mask & (1 << index):
                    adjacency[u].add(v)
                    adjacency[v].add(u)
            for fire_mask in range(1, 1 << n):
                initial = frozenset(v for v in range(n) if fire_mask & (1 << v))
                if n - len(initial) >= 2:
                    instances.append(Instance(tuple(map(frozenset, adjacency)), initial))
    for n in (5, 6):
        for _ in range(12):
            adjacency = [set() for _ in range(n)]
            for u, v in combinations(range(n), 2):
                if rng.random() < 0.35:
                    adjacency[u].add(v)
                    adjacency[v].add(u)
            initial = {vertex for vertex in range(n) if rng.random() < 0.2}
            if not initial:
                initial.add(rng.randrange(n))
            if n - len(initial) >= 2:
                instances.append(Instance(tuple(map(frozenset, adjacency)), frozenset(initial)))

    checked = 0
    for case, instance in enumerate(instances):
        max_horizon = min(instance.n - 2, 4)
        for horizon in range(1, max_horizon + 1):
            saved_target = horizon + 2
            burned_bound = instance.n - saved_target
            nonburning = sorted(set(range(instance.n)) - set(instance.initial_fire))
            for pair in combinations(nonburning, 2):
                pair_set = set(pair)
                base = _solve_temporal_query(
                    instance,
                    1,
                    horizon,
                    "cadical300",
                    "pair-base-regression",
                    pair_set,
                    burned_bound=burned_bound,
                )
                exact = _solve_temporal_query(
                    instance,
                    1,
                    horizon,
                    "cadical300",
                    "pair-exact-regression",
                    pair_set,
                    burned_bound=burned_bound,
                )
                if base["result"] != exact["result"]:
                    raise AssertionError(
                        "PAIR_CANONICALIZATION_FAILED: "
                        f"case={case}, n={instance.n}, B={sorted(instance.initial_fire)}, "
                        f"T={horizon}, pair={pair}, base={base['result']}, exact={exact['result']}"
                    )
                checked += 1
    return {
        "passed": True,
        "seed": seed,
        "graph_cases": len(instances),
        "pair_queries_checked": checked,
        "scope": "all graphs and nonempty fires through n=4; seeded n=5..6",
    }


def _validate_separator_input(instance, horizon, witness_path):
    payload = json.loads(Path(witness_path).read_text(encoding="utf-8"))
    if isinstance(payload, dict) and "target_query" in payload:
        payload = payload["target_query"].get("witness", {})
    elif isinstance(payload, dict) and "witness" in payload:
        payload = payload["witness"]
    separator = set(payload.get("separator", ()))
    pair = set(payload.get("canonical_safe_region", payload.get("safe_region", ())))
    if len(separator) != horizon or len(pair) != 2:
        raise ValueError(
            f"Expected a separator of size {horizon} and a 2-vertex safe pair; "
            f"got |R|={len(separator)}, |U|={len(pair)}"
        )
    if not pair.isdisjoint(separator) or (separator | pair) & set(instance.initial_fire):
        raise ValueError("Separator witness overlaps itself or an initial fire vertex")
    validated = validate_static_witness(
        instance,
        horizon,
        horizon + 2,
        separator,
        pair,
    )
    return {
        "separator": sorted(separator),
        "untouched_pair": sorted(pair),
        "validated": True,
        "static_validation": {
            "separator_size": validated["separator_size"],
            "safe_region": validated["canonical_safe_region"],
            "static_saved": validated["static_saved"],
        },
    }


def _count_boundary_feasible_pairs(instance, defense_budget):
    initial = set(instance.initial_fire)
    available = [vertex for vertex in range(instance.n) if vertex not in initial]
    count = 0
    candidates = []
    for u, v in combinations(available, 2):
        boundary = (set(instance.adjacency[u]) | set(instance.adjacency[v])) - {u, v}
        if boundary.isdisjoint(initial) and len(boundary) <= defense_budget:
            count += 1
            candidates.append({"pair": [u, v], "boundary": sorted(boundary)})
    return {
        "enumerated": True,
        "filter_is_complete": True,
        "all_pairs": instance.n * (instance.n - 1) // 2,
        "nonburning_pairs": len(available) * (len(available) - 1) // 2,
        "boundary_feasible_pairs": count,
        "candidates": candidates,
    }


def _temporal_query_worker(connection, path, firefighters, horizon, solver_name, mode, pair, separator):
    try:
        instance = read_instance(path)
        result = _solve_temporal_query(
            instance,
            firefighters,
            horizon,
            solver_name,
            mode,
            set(pair),
            separator=set(separator) if separator is not None else None,
        )
        connection.send(("RESULT", result))
    except Exception as exc:
        connection.send(("RESULT", {"mode": mode, "result": "ERROR", "error": f"{type(exc).__name__}: {exc}"}))
    finally:
        connection.close()


def _run_temporal_query(path, firefighters, horizon, solver_name, mode, pair, separator, time_limit):
    """Run a fresh temporal query under an independent hard wall-clock limit."""
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(
        target=_temporal_query_worker,
        args=(sender, str(path), firefighters, horizon, solver_name, mode, sorted(pair),
              sorted(separator) if separator is not None else None),
    )
    process.start()
    sender.close()
    started = time.monotonic()
    result = None
    timed_out = False
    try:
        while True:
            remaining = time_limit - (time.monotonic() - started)
            if remaining <= 0:
                timed_out = process.is_alive() and result is None
                break
            if receiver.poll(min(0.05, remaining)):
                try:
                    kind, result = receiver.recv()
                except (EOFError, OSError):
                    if not process.is_alive():
                        break
                    continue
                if kind == "RESULT":
                    process.join(0.1)
                    if not process.is_alive():
                        break
            elif not process.is_alive():
                break
        timed_out = timed_out or (result is None and process.is_alive())
        if process.is_alive():
            if timed_out:
                process.terminate()
                process.join(0.5)
            else:
                process.join(0.5)
            if process.is_alive():
                process.kill()
                process.join(0.5)
        while receiver.poll():
            try:
                kind, payload = receiver.recv()
            except (EOFError, OSError):
                break
            if kind == "RESULT":
                result = payload
        if timed_out:
            result = {"mode": mode, "result": "TIMEOUT", "timeout_seconds": time_limit}
        elif result is None:
            result = {"mode": mode, "result": "ERROR", "error": f"Worker exited with code {process.exitcode}"}
        result["wall_time"] = time.monotonic() - started
        result["timeout_seconds"] = time_limit
        return result
    finally:
        if process.is_alive():
            process.kill()
            process.join(0.5)
        receiver.close()
        process.close()


def run_static_dynamic_bridge(
    path,
    firefighters,
    horizon,
    burned_bound,
    separator_witness_path,
    static_next_target=12,
    static_time=600.0,
    dynamic_time=600.0,
    solver_name="cadical300",
):
    if firefighters != 1:
        raise ValueError("static-dynamic-bridge currently requires --firefighters 1")
    if horizon <= 0 or burned_bound < 0:
        raise ValueError("Invalid bridge query")
    instance = read_instance(path)
    saved_target = instance.n - burned_bound
    if saved_target != horizon + 2:
        raise ValueError("Pair-exact diagnostic requires n-K = T+2")
    if static_next_target <= horizon + 2:
        raise ValueError("--static-next-target must exceed the known static target T+2")

    pair_regression = _small_graph_pair_regression()
    witness = _validate_separator_input(instance, horizon, separator_witness_path)
    separator = set(witness["separator"])
    pair = set(witness["untouched_pair"])

    static_extension = run_static_separator(
        path,
        firefighters,
        horizon,
        instance.n - static_next_target,
        solver_name,
        static_time,
    )
    static_extension["target_11_known_sat"] = witness["static_validation"]["static_saved"] >= 11
    static_extension["static_optimum"] = (
        static_next_target - 1 if static_extension["result"] == "UNSAT" else None
    )

    exact_separator = _run_temporal_query(
        path,
        firefighters,
        horizon,
        solver_name,
        "exact-separator",
        pair,
        separator,
        dynamic_time,
    )
    if exact_separator["result"] != "SAT":
        pair_only = _run_temporal_query(
            path,
            firefighters,
            horizon,
            solver_name,
            "pair-only",
            pair,
            None,
            dynamic_time,
        )
    else:
        pair_only = {"result": "SKIPPED_EXACT_SEPARATOR_SAT"}

    pair_space = None
    if static_extension["result"] == "UNSAT":
        pair_space = _count_boundary_feasible_pairs(instance, horizon * firefighters)

    return {
        "query": {
            "T": horizon,
            "K": burned_bound,
            "defense_budget": horizon * firefighters,
            "saved_target": saved_target,
        },
        "pair_canonicalization_regression": pair_regression,
        "static_extension": static_extension,
        "input_witness": witness,
        "exact_separator": exact_separator,
        "pair_only": pair_only,
        "pair_space": pair_space,
    }


def write_bridge_outputs(report, out_dir, stem="static_dynamic_bridge"):
    output = Path(out_dir)
    output.mkdir(parents=True, exist_ok=True)
    json_path = output / f"{stem}.json"
    csv_path = output / f"{stem}.csv"
    json_path.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    rows = []
    static = report["static_extension"]
    rows.append({"stage": "static-12", **_csv_query(static)})
    rows.append({"stage": "exact-separator", **_csv_query(report["exact_separator"])})
    rows.append({"stage": "pair-only", **_csv_query(report["pair_only"])})
    fields = (
        "stage",
        "target",
        "result",
        "solve_time",
        "variables",
        "clauses",
        "actual_k",
        "containment_time",
        "separator_size",
        "canonical_safe_size",
        "static_saved",
        "decisions",
        "conflicts",
        "propagations",
        "restarts",
    )
    with csv_path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return json_path, csv_path


def _csv_query(query):
    stats = query.get("stats", {})
    witness = query.get("witness", {})
    return {
        "target": query.get("target"),
        "result": query.get("result"),
        "solve_time": query.get("solve_time"),
        "variables": query.get("total_variables", query.get("variables")),
        "clauses": query.get("clauses"),
        "actual_k": query.get("actual_k"),
        "containment_time": query.get("containment_time"),
        "separator_size": witness.get("separator_size"),
        "canonical_safe_size": witness.get("canonical_safe_size"),
        "static_saved": witness.get("static_saved"),
        **stats,
    }
