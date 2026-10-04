"""Diagnostic Benders pilot linking a static separator master to temporal SAT.

For the canonical target n-K = T+2 with D=1, every feasible dynamic strategy
can be padded to exactly T defended vertices and leaves at least two untouched
vertices. Hence the master separator/safe-region constraints are necessary,
and a temporal UNSAT core over d[v,T] yields a globally valid no-good cut.
"""

from __future__ import annotations

import csv
import json
import math
import multiprocessing
import random
import time
from collections import Counter
from itertools import combinations
from pathlib import Path

from pysat.solvers import Solver

from .encoder import Encoder
from .instance import read_instance
from .objective import IncrementalAtLeastCounter
from .preprocess import preprocess
from .simulator import simulate
from .static_separator import _reachable_without
from .totalizer import AtMost
from .variables import VarManager


def _stats_delta(solver, before):
    after = solver.accum_stats()
    return {
        key: after.get(key, 0) - before.get(key, 0)
        for key in ("decisions", "conflicts", "propagations", "restarts")
    }


def _temporal_formula(instance, horizon, solver_name):
    distance, _ = preprocess(instance, 1)
    solver = Solver(name=solver_name)
    encoder = Encoder(instance, 1, solver, distance)
    encoder.ensure_horizon(horizon)
    containment = encoder.ensure_containment(horizon)
    solver.add_clause([containment])
    return solver, encoder


def _minimize_assumption_core(solver, raw_core, reverse, order, soft_limit):
    rank = {vertex: index for index, vertex in enumerate(order)}
    core = sorted(set(raw_core), key=lambda lit: rank[reverse[lit]])
    raw_vertices = [reverse[lit] for lit in core]
    calls = 0
    over_limit = 0
    elapsed_total = 0.0
    changed = True
    subset_minimal = True
    while changed:
        changed = False
        pass_over_limit = False
        for lit in list(core):
            trial = [item for item in core if item != lit]
            started = time.monotonic()
            result = solver.solve(assumptions=trial)
            spent = time.monotonic() - started
            calls += 1
            elapsed_total += spent
            if spent > soft_limit:
                over_limit += 1
                pass_over_limit = True
                continue
            if result is not False:
                continue
            smaller = solver.get_core()
            if smaller is None or not smaller or not set(smaller).issubset(set(trial)):
                raise ValueError("Core minimization returned an invalid reduced core")
            core = sorted(set(smaller), key=lambda item: rank[reverse[item]])
            changed = True
            break
        if not changed:
            subset_minimal = not pass_over_limit
    return {
        "raw": raw_vertices,
        "reduced": [reverse[lit] for lit in core],
        "raw_literals": raw_core,
        "reduced_literals": core,
        "subset_minimal": subset_minimal,
        "minimize_calls": calls,
        "minimize_time": elapsed_total,
        "calls_over_soft_limit": over_limit,
    }


def _assumption_orders(vertices, restarts, seed=0):
    current = list(vertices)
    variants = [
        sorted(vertices),
        sorted(vertices, reverse=True),
        current,
        list(reversed(current)),
    ]
    variants.extend(random.Random(seed + index).sample(current, len(current)) for index in range(4))
    return [variants[index % len(variants)] for index in range(restarts)]


def _mine_propagation_cores(solver, encoder, vertices, horizon, max_size):
    found = []
    tested = Counter()
    conflicts = Counter()
    ordered = sorted(vertices)
    for size in range(1, min(max_size, len(ordered)) + 1):
        for subset in combinations(ordered, size):
            subset_set = frozenset(subset)
            if any(core <= subset_set for core in found):
                continue
            assumptions = [encoder.d[v, horizon] for v in subset]
            status, _propagated = solver.propagate(assumptions=assumptions)
            if status not in (True, False):
                raise ValueError(f"Unexpected propagate() status {status!r}")
            tested[size] += 1
            if status is False:
                conflicts[size] += 1
                found = [core for core in found if not subset_set < core]
                found.append(subset_set)
    return {
        "cores": [sorted(core) for core in sorted(found, key=lambda core: (len(core), tuple(sorted(core))))],
        "tested": {str(size): tested[size] for size in range(1, max_size + 1)},
        "conflicts": {str(size): conflicts[size] for size in range(1, max_size + 1)},
    }


def _load_seed_witnesses(paths, instance, defense_budget):
    seeds = []

    def visit(value, source):
        if isinstance(value, dict):
            if isinstance(value.get("separator"), list):
                separator = sorted(set(map(int, value["separator"])))
                raw_safe = value.get("canonical_safe_region", value.get("safe_region", []))
                raw_safe = sorted(set(map(int, raw_safe)))
                if any(v < 0 or v >= instance.n for v in separator + raw_safe):
                    raise ValueError(f"Seed {source} contains an out-of-range vertex")
                if len(separator) != defense_budget:
                    raise ValueError(f"Seed {source} must contain exactly {defense_budget} separator vertices")
                if set(separator) & set(instance.initial_fire):
                    raise ValueError(f"Seed {source} separator contains an initial burning vertex")
                reachable = _reachable_without(instance, separator)
                if set(raw_safe) & set(reachable):
                    raise ValueError(f"Seed {source} claims a reachable vertex is safe")
                safe = sorted(set(range(instance.n)) - set(separator) - reachable)
                if len(safe) < 2:
                    raise ValueError(f"Seed {source} has fewer than two canonical safe vertices")
                if set(raw_safe) & (set(separator) | set(instance.initial_fire)):
                    raise ValueError(f"Seed {source} has an invalid raw safe region")
                seeds.append({"source": source, "separator": separator, "raw_safe_region": raw_safe,
                              "safe_region": safe, "safe_size": len(safe)})
                return
            for key, child in value.items():
                visit(child, f"{source}:{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, f"{source}:{index}")

    for path in paths or []:
        visit(json.loads(Path(path).read_text(encoding="utf-8")), str(path))
    unique = {}
    for seed in seeds:
        unique.setdefault(tuple(seed["separator"]), seed)
    return sorted(unique.values(), key=lambda row: (-row["safe_size"], row["source"], row["separator"]))


def _load_replay_separator(spec, instance, defense_budget):
    if not spec:
        return None
    try:
        path_text, iteration_text = spec.rsplit(":", 1)
        iteration = int(iteration_text)
    except (ValueError, AttributeError) as exc:
        raise ValueError("--replay-separator-from must be PATH:ITERATION") from exc
    report = json.loads(Path(path_text).read_text(encoding="utf-8"))
    row = next((item for item in report.get("iterations", [])
                if int(item.get("iteration", -1)) == iteration), None)
    if row is None:
        raise ValueError(f"No iteration {iteration} in replay report {path_text}")
    separator = sorted(set(map(int, row.get("separator", []))))
    if len(separator) != defense_budget:
        raise ValueError("Replayed separator has the wrong cardinality")
    if any(v < 0 or v >= instance.n for v in separator):
        raise ValueError("Replayed separator contains an out-of-range vertex")
    if set(separator) & set(instance.initial_fire):
        raise ValueError("Replayed separator contains an initial burning vertex")
    reachable = _reachable_without(instance, separator)
    safe = sorted(set(range(instance.n)) - set(separator) - reachable)
    if len(safe) < 2:
        raise ValueError("Replayed separator has fewer than two canonical safe vertices")
    return {
        "source": f"replay:{path_text}:{iteration}",
        "separator": separator,
        "raw_safe_region": row.get("raw_safe_region", []),
        "safe_region": safe,
        "safe_size": len(safe),
    }


def _build_master(instance, defense_budget, solver):
    manager = VarManager()
    r = [manager.new(f"r[{v}]") for v in range(instance.n)]
    s = [manager.new(f"s[{v}]") for v in range(instance.n)]
    clauses = 0

    def add(clause):
        nonlocal clauses
        solver.add_clause(list(clause))
        clauses += 1

    for v in instance.initial_fire:
        add([-r[v]])
        add([-s[v]])
    for v in range(instance.n):
        add([-r[v], -s[v]])
    for u, neighbors in enumerate(instance.adjacency):
        for v in neighbors:
            if u < v:
                add([-s[u], s[v], r[v]])
                add([-s[v], s[u], r[u]])

    r_at_most = AtMost(r, defense_budget, manager)
    for clause in r_at_most.clauses:
        add(clause)
    r_exact_counter = IncrementalAtLeastCounter(r, manager, add, horizon="benders_r")
    r_exact = r_exact_counter.assumption(defense_budget)
    r_at_most_lit = r_at_most.assumption(defense_budget)

    s_counter = IncrementalAtLeastCounter(s, manager, add, horizon="benders_s")
    s_at_least = s_counter.assumption(2)
    return {
        "manager": manager, "r": r, "s": s, "assumptions": [r_at_most_lit, r_exact, s_at_least],
        "base_clauses": clauses, "clause_count": clauses,
        "r_at_most": r_at_most, "r_counter": r_exact_counter,
        "s_counter": s_counter,
    }


def _decode_master(solver, formula, instance, defense_budget):
    model = set(lit for lit in solver.get_model() if lit > 0)
    separator = sorted(v for v, lit in enumerate(formula["r"]) if lit in model)
    raw_safe = sorted(v for v, lit in enumerate(formula["s"]) if lit in model)
    if len(separator) != defense_budget:
        raise AssertionError(f"Master returned |R|={len(separator)}, expected {defense_budget}")
    reachable = _reachable_without(instance, separator)
    safe = sorted(set(range(instance.n)) - set(separator) - reachable)
    if len(safe) < 2:
        raise AssertionError("Master model has fewer than two graph-theoretically safe vertices")
    return separator, raw_safe, safe


def _temporal_candidate_impl(instance, separator, horizon, solver_name, minimize_time, connection=None):
    distance, _ = preprocess(instance, 1)
    with Solver(name=solver_name) as solver:
        encoder = Encoder(instance, 1, solver, distance)
        try:
            encoder.ensure_horizon(horizon)
            containment = encoder.ensure_containment(horizon)
            solver.add_clause([containment])
            assumptions = [encoder.d[v, horizon] for v in separator]
            base_metadata = {
                "variables": encoder.vars.top,
                "clauses": encoder.clauses + 1,
                "assumption_count": len(assumptions),
                "assumptions": assumptions,
                "objective_variables": 0,
                "objective_clauses": 0,
            }
            before = solver.accum_stats()
            solve_started = time.monotonic()
            satisfiable = solver.solve(assumptions=assumptions)
            elapsed = time.monotonic() - solve_started
            stats = _stats_delta(solver, before)
            if satisfiable:
                model = set(solver.get_model())
                schedule = [list(actions) for actions in encoder.decode(model, horizon)]
                solution = simulate(instance, 1, schedule)
                defended = sorted(v for v in range(instance.n) if encoder.d[v, horizon] in model)
                if defended != sorted(separator):
                    raise AssertionError("SAT temporal model defended set differs from the fixed separator")
                safe = set(range(instance.n)) - set(separator) - _reachable_without(instance, separator)
                if safe & set(solution.burned):
                    raise AssertionError("A canonical safe vertex burned in the temporal schedule")
                if solution.k > instance.n - (len(separator) + len(safe)):
                    raise AssertionError("Temporal model contradicts its static safe region")
                return {
                    **base_metadata, "result": "SAT", "solve_time": elapsed, "stats": stats,
                    "actual_k": solution.k, "containment_time": solution.containment_time,
                    "schedule": [list(actions) for actions in solution.schedule],
                    "defended": defended, "canonical_safe_region": sorted(safe), "validated": True,
                }

            raw_core = solver.get_core()
            if raw_core is None or not raw_core:
                return {**base_metadata, "result": "INVALID_TEMPORAL_CORE", "solve_time": elapsed,
                        "stats": stats, "raw_core": raw_core}
            if not set(raw_core).issubset(set(assumptions)):
                return {**base_metadata, "result": "INVALID_TEMPORAL_CORE", "solve_time": elapsed,
                        "stats": stats, "raw_core": raw_core,
                        "error": "Core contains a literal outside the fixed-separator assumptions"}
            reverse = {encoder.d[v, horizon]: v for v in separator}
            raw_vertices = sorted({reverse[lit] for lit in raw_core})
            if not raw_vertices or not set(raw_vertices).issubset(separator):
                return {**base_metadata, "result": "INVALID_TEMPORAL_CORE", "solve_time": elapsed,
                        "stats": stats, "raw_core": raw_core}

            core = list(raw_core)
            if connection is not None:
                connection.send(("CORE_PROGRESS", {**base_metadata, "stats": stats,
                                                     "raw_core": raw_vertices, "reduced_core": raw_vertices}))
            calls = 0
            minimize_timeouts = 0
            minimize_elapsed = 0.0
            complete = True
            changed = True
            while changed:
                changed = False
                pass_had_timeout = False
                for lit in list(core):
                    if lit not in core:
                        continue
                    trial = [x for x in core if x != lit]
                    before_min = solver.accum_stats()
                    trial_started = time.monotonic()
                    minimized = solver.solve(assumptions=trial)
                    spent = time.monotonic() - trial_started
                    calls += 1
                    minimize_elapsed += spent
                    if spent > minimize_time:
                        minimize_timeouts += 1
                        pass_had_timeout = True
                    elif minimized is False:
                        smaller = solver.get_core()
                        if smaller is None or not smaller or not set(smaller).issubset(set(trial)):
                            raise AssertionError("Core minimization returned an invalid reduced core")
                        if len(smaller) < len(core):
                            core = list(dict.fromkeys(smaller))
                            changed = True
                            complete = True
                            if connection is not None:
                                connection.send(("CORE_PROGRESS", {
                                    **base_metadata, "stats": stats,
                                    "raw_core": raw_vertices,
                                    "reduced_core": sorted({reverse[item] for item in core}),
                                }))
                            break
                        core = trial
                        changed = True
                        break
                    _ = _stats_delta(solver, before_min)
                if not changed:
                    complete = not pass_had_timeout
            final_vertices = sorted({reverse[lit] for lit in core})
            if not final_vertices or not set(final_vertices).issubset(separator):
                raise AssertionError("Reduced core is not a nonempty semantic subset of R")
            return {
                **base_metadata, "result": "UNSAT", "solve_time": elapsed, "stats": stats,
                "raw_core_literals": raw_core, "raw_core": raw_vertices,
                "reduced_core_literals": core, "reduced_core": final_vertices,
                "subset_minimal": complete,
                "core_minimize_calls": calls, "core_minimize_time": minimize_elapsed,
                "core_minimize_calls_over_soft_limit": minimize_timeouts,
                "core_minimize_soft_limit_seconds": minimize_time,
                "hard_interrupt_supported": False,
                "candidate_cores": [{"source_type": "deletion-core", "restart_index": 0,
                                      "raw": raw_vertices, "reduced": final_vertices,
                                      "subset_minimal": complete}],
            }
        finally:
            encoder.close()


def _core_start(instance, separator, horizon, solver_name, order, restart_index, soft_limit):
    encode_started = time.monotonic()
    solver, encoder = _temporal_formula(instance, horizon, solver_name)
    encoding_time = time.monotonic() - encode_started
    try:
        assumptions = [encoder.d[v, horizon] for v in order]
        before = solver.accum_stats()
        started = time.monotonic()
        result = solver.solve(assumptions=assumptions)
        elapsed = time.monotonic() - started
        stats = _stats_delta(solver, before)
        if result is True:
            model = set(solver.get_model())
            schedule = [list(actions) for actions in encoder.decode(model, horizon)]
            solution = simulate(instance, 1, schedule)
            defended = sorted(v for v in range(instance.n) if encoder.d[v, horizon] in model)
            if defended != sorted(separator):
                raise AssertionError("SAT restart defended set differs from its fixed separator")
            return {"result": "SAT", "encoding_time": encoding_time,
                    "solve_time": elapsed, "stats": stats,
                    "actual_k": solution.k, "containment_time": solution.containment_time,
                    "schedule": [list(actions) for actions in solution.schedule], "defended": defended,
                    "validated": True}
        raw = solver.get_core()
        if raw is None or not raw or not set(raw).issubset(set(assumptions)):
            raise ValueError("Restart returned an invalid assumption core")
        reverse = {encoder.d[v, horizon]: v for v in separator}
        minimized = _minimize_assumption_core(solver, raw, reverse, order, soft_limit)
        return {
            "result": "UNSAT", "encoding_time": encoding_time,
            "solve_time": elapsed, "stats": stats,
            "restart_index": restart_index,
            **minimized,
            "source_type": "deletion-core",
        }
    finally:
        encoder.close()
        solver.delete()


def _internal_core_reduce(records):
    retained = []
    for record in sorted(records, key=lambda row: (len(row["reduced"]), tuple(row["reduced"]))):
        core = frozenset(record["reduced"])
        if any(frozenset(old["reduced"]) <= core for old in retained):
            continue
        retained = [old for old in retained if not core < frozenset(old["reduced"])]
        retained.append(record)
    return retained


def _temporal_candidate_multi_impl(
    instance, separator, horizon, solver_name, subset_scan_max_size,
    core_restarts, core_restart_stop_size, calibration_full_solve,
    candidate_index, soft_limit, connection=None,
):
    encode_started = time.monotonic()
    solver, encoder = _temporal_formula(instance, horizon, solver_name)
    encoding_time = time.monotonic() - encode_started
    metadata = {
        "variables": encoder.vars.top,
        "clauses": encoder.clauses + 1,
        "assumption_count": len(separator),
        "objective_variables": 0,
        "objective_clauses": 0,
    }
    try:
        propagation_started = time.monotonic()
        scan = _mine_propagation_cores(
            solver, encoder, separator, horizon, subset_scan_max_size
        )
        propagation_scan_time = time.monotonic() - propagation_started
        records = [
            {"source_type": "propagation", "restart_index": None,
             "raw": core, "reduced": core, "subset_minimal": True}
            for core in scan["cores"]
        ]
        if connection is not None and records:
            connection.send(("CORE_PROGRESS", {
                **metadata, "propagation_cores": scan["cores"],
                "candidate_cores": _internal_core_reduce(records),
            }))

        need_full = not records or candidate_index <= calibration_full_solve
        full_solved = False
        restart_attempts = 0
        total_solve_time = 0.0
        total_encoding_time = encoding_time
        aggregate_stats = Counter()
        if need_full:
            order = list(separator)
            assumptions = [encoder.d[v, horizon] for v in order]
            before = solver.accum_stats()
            started = time.monotonic()
            result = solver.solve(assumptions=assumptions)
            spent = time.monotonic() - started
            full_solved = True
            restart_attempts += 1
            total_solve_time += spent
            aggregate_stats.update(_stats_delta(solver, before))
            if result is True:
                if records:
                    raise AssertionError("Propagation reported a cut but full solve found SAT")
                model = set(solver.get_model())
                schedule = [list(actions) for actions in encoder.decode(model, horizon)]
                solution = simulate(instance, 1, schedule)
                defended = sorted(v for v in range(instance.n) if encoder.d[v, horizon] in model)
                if defended != sorted(separator):
                    raise AssertionError("SAT model defended set differs from the fixed separator")
                return {
                    **metadata, "result": "SAT", "encoding_time": encoding_time,
                    "propagation_scan_time": propagation_scan_time,
                    "solve_time": total_solve_time, "stats": dict(aggregate_stats),
                    "actual_k": solution.k, "containment_time": solution.containment_time,
                    "schedule": [list(actions) for actions in solution.schedule], "defended": defended,
                    "validated": True, "propagation_scan": scan, "full_temporal_solved": True,
                    "restart_attempts": restart_attempts, "candidate_cores": [],
                }
            raw = solver.get_core()
            if raw is None or not raw or not set(raw).issubset(set(assumptions)):
                raise ValueError("Full temporal solve returned an invalid assumption core")
            reverse = {encoder.d[v, horizon]: v for v in separator}
            minimized = _minimize_assumption_core(solver, raw, reverse, order, soft_limit)
            records.append({
                "source_type": "deletion-core", "restart_index": 0,
                "raw": minimized["raw"], "reduced": minimized["reduced"],
                "subset_minimal": minimized["subset_minimal"],
                "minimize_calls": minimized["minimize_calls"],
                "minimize_time": minimized["minimize_time"],
                "calls_over_soft_limit": minimized["calls_over_soft_limit"],
            })
            if connection is not None:
                connection.send(("CORE_PROGRESS", {
                    **metadata, "propagation_cores": scan["cores"],
                    "candidate_cores": _internal_core_reduce(records),
                }))

        orders = _assumption_orders(separator, core_restarts, seed=0)
        start_index = 1 if need_full else 0
        if records and min(len(row["reduced"]) for row in records) <= core_restart_stop_size:
            orders = []
        for restart_index in range(start_index, len(orders)):
            order = orders[restart_index]
            attempt = _core_start(
                instance, separator, horizon, solver_name, order,
                restart_index, soft_limit,
            )
            restart_attempts += 1
            total_solve_time += attempt["solve_time"]
            total_encoding_time += attempt.get("encoding_time", 0.0)
            aggregate_stats.update(attempt.get("stats", {}))
            if attempt["result"] == "SAT":
                if records:
                    raise AssertionError("A proven UNSAT assumption subset was also SAT in another restart")
                return {
                    **metadata, **attempt, "encoding_time": total_encoding_time,
                    "propagation_scan_time": propagation_scan_time,
                    "propagation_scan": scan, "full_temporal_solved": full_solved,
                    "restart_attempts": restart_attempts, "candidate_cores": [],
                }
            records.append({
                "source_type": attempt["source_type"], "restart_index": restart_index,
                "raw": attempt["raw"], "reduced": attempt["reduced"],
                "subset_minimal": attempt["subset_minimal"],
                "minimize_calls": attempt["minimize_calls"],
                "minimize_time": attempt["minimize_time"],
                "calls_over_soft_limit": attempt["calls_over_soft_limit"],
            })
            if connection is not None:
                connection.send(("CORE_PROGRESS", {
                    **metadata, "propagation_cores": scan["cores"],
                    "candidate_cores": _internal_core_reduce(records),
                }))
            if min(len(row["reduced"]) for row in records) <= core_restart_stop_size:
                break

        reduced_records = _internal_core_reduce(records)
        return {
            **metadata, "result": "UNSAT", "encoding_time": total_encoding_time,
            "propagation_scan_time": propagation_scan_time,
            "solve_time": total_solve_time, "stats": dict(aggregate_stats),
            "propagation_scan": scan, "propagation_cores": scan["cores"],
            "full_temporal_solved": full_solved, "restart_attempts": restart_attempts,
            "restart_cores": records, "candidate_cores": reduced_records,
            "raw_cores_unique": len({tuple(row["raw"]) for row in records}),
            "reduced_cores_unique": len({tuple(row["reduced"]) for row in records}),
            "smallest_core_size": min((len(row["reduced"]) for row in reduced_records), default=None),
            "core_minimize_soft_limit_seconds": soft_limit,
            "core_minimize_calls_over_soft_limit": sum(row.get("calls_over_soft_limit", 0) for row in records),
            "hard_interrupt_supported": False,
        }
    finally:
        encoder.close()
        solver.delete()


def _temporal_candidate_worker(
    connection, instance, separator, horizon, solver_name, soft_limit,
    mode="single-core", subset_scan_max_size=3, core_restarts=8,
    core_restart_stop_size=2, calibration_full_solve=0, candidate_index=1,
):
    try:
        if mode == "multi-small-core":
            result = _temporal_candidate_multi_impl(
                instance, separator, horizon, solver_name, subset_scan_max_size,
                core_restarts, core_restart_stop_size, calibration_full_solve,
                candidate_index, soft_limit, connection,
            )
        else:
            result = _temporal_candidate_impl(instance, separator, horizon, solver_name, soft_limit, connection)
        connection.send(("RESULT", result))
    except BaseException as exc:
        connection.send(("ERROR", {"result": "ERROR", "error": f"{type(exc).__name__}: {exc}"}))
    finally:
        connection.close()


def _temporal_candidate(
    instance, separator, horizon, solver_name, timeout, minimize_time,
    mode="single-core", subset_scan_max_size=3, core_restarts=8,
    core_restart_stop_size=2, calibration_full_solve=0, candidate_index=1,
):
    """Use a fresh process so a hard timeout cannot corrupt another solver."""
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe(duplex=False)
    process = context.Process(
        target=_temporal_candidate_worker,
        args=(
            child, instance, list(separator), horizon, solver_name, minimize_time,
            mode, subset_scan_max_size, core_restarts, core_restart_stop_size,
            calibration_full_solve, candidate_index,
        ),
    )
    started = time.monotonic()
    process.start()
    child.close()
    latest_core = None
    final = None
    while time.monotonic() - started < timeout:
        wait = min(0.1, max(0.0, timeout - (time.monotonic() - started)))
        if parent.poll(wait):
            kind, payload = parent.recv()
            if kind == "CORE_PROGRESS":
                latest_core = payload
            elif kind == "RESULT":
                final = payload
                break
            elif kind == "ERROR":
                final = payload
                break
        elif not process.is_alive():
            break
    elapsed = time.monotonic() - started
    if final is None and process.is_alive():
        process.terminate()
        process.join()
    else:
        process.join(timeout=1.0)
    parent.close()
    if final is not None:
        return final
    if latest_core is not None:
        # A published propagation conflict or assumption core is already a
        # proof. Keep it if the worker hits its outer wall limit during later
        # restarts/minimization; a timeout itself never creates a cut.
        if "candidate_cores" in latest_core:
            candidate_cores = latest_core["candidate_cores"]
            return {
                **latest_core, "result": "UNSAT", "solve_time": elapsed,
                "candidate_cores": candidate_cores,
                "worker_timeout_after_core": True,
                "core_minimize_calls_over_soft_limit": latest_core.get(
                    "core_minimize_calls_over_soft_limit", 0
                ),
            }
        return {
            **latest_core,
            "result": "UNSAT", "solve_time": elapsed,
            "raw_core": latest_core.get("raw_core"),
            "reduced_core": latest_core.get("reduced_core"),
            "subset_minimal": False,
            "core_minimize_calls": 0, "core_minimize_time": 0.0,
            "core_minimize_calls_over_soft_limit": 0,
            "core_minimize_soft_limit_seconds": minimize_time,
            "hard_interrupt_supported": False,
            "worker_timeout_after_core": True,
        }
    return {"result": "TIMEOUT", "solve_time": elapsed, "stats": {},
            "assumption_count": len(separator), "assumptions": [], "objective_variables": 0,
            "objective_clauses": 0}


def _coverage(cut_size, n, defense_budget):
    if cut_size > defense_budget:
        return {"fraction": 0.0, "log10_fraction": None}
    log_numerator = (
        math.lgamma(n - cut_size + 1)
        - math.lgamma(defense_budget - cut_size + 1)
        - math.lgamma(n - defense_budget + 1)
    )
    log_denominator = (
        math.lgamma(n + 1)
        - math.lgamma(defense_budget + 1)
        - math.lgamma(n - defense_budget + 1)
    )
    log_fraction = log_numerator - log_denominator
    return {"fraction": math.exp(log_fraction) if log_fraction > -745 else 0.0,
            "log10_fraction": log_fraction / math.log(10)}


def run_separator_benders_pilot(
    path, firefighters, horizon, burned_bound, solver_name="cadical300", max_iterations=64,
    master_query_time=30.0, subproblem_time=10.0, subproblem_retry_time=60.0,
    core_minimize_time=2.0, total_time=1200.0, seed_witnesses=None,
    mode="single-core", subset_scan_max_size=3, core_restarts=8,
    core_restart_stop_size=2, calibration_full_solve=16, replay_separator_from=None,
):
    if firefighters != 1:
        raise ValueError("separator-benders-pilot currently supports only --firefighters 1")
    instance = read_instance(path)
    if burned_bound > instance.n or burned_bound < 0:
        raise ValueError("K must be between zero and n")
    defense_budget = firefighters * horizon
    saved_target = instance.n - burned_bound
    if saved_target != defense_budget + 2:
        raise ValueError("separator-benders-pilot requires n-K = T*D + 2 for the canonical target")
    started = time.monotonic()
    if mode not in {"single-core", "multi-small-core"}:
        raise ValueError(f"Unsupported Benders core mode: {mode}")
    if mode == "multi-small-core" and subset_scan_max_size not in range(1, defense_budget + 1):
        raise ValueError("subset scan size must be between 1 and the defense budget")
    if core_restarts < 1 or core_restart_stop_size < 1:
        raise ValueError("core restarts and stop size must be positive")
    replay_seed = _load_replay_separator(replay_separator_from, instance, defense_budget)
    seeds = _load_seed_witnesses(seed_witnesses, instance, defense_budget)
    report = {
        "query": {"T": horizon, "K": burned_bound, "defense_budget": defense_budget,
                  "saved_target": saved_target, "firefighters": firefighters},
        "configuration": {"solver": solver_name, "mode": mode, "max_iterations": max_iterations,
                          "master_query_time": master_query_time, "subproblem_time": subproblem_time,
                          "subproblem_retry_time": subproblem_retry_time,
                          "core_minimize_soft_limit_seconds": core_minimize_time,
                          "subset_scan_max_size": subset_scan_max_size,
                          "core_restarts": core_restarts,
                          "core_restart_stop_size": core_restart_stop_size,
                          "calibration_full_solve": calibration_full_solve,
                          "replay_separator_from": replay_separator_from, "total_time": total_time,
                          "master_query_timeout_mode": "soft; Cadical300 limited-solve interruption is unsupported",
                          "subproblem_timeout_mode": "hard process timeout",
                          "core_minimize_timeout_mode": "posthoc per-call elapsed-time check; a CaDiCaL call cannot be preempted while retaining its learned state"},
        "backend_capabilities": {
            "assumption_core": True,
            "limited_solve": False if solver_name.startswith("cadical") else "not preflighted",
            "master_wall_timeout": "cannot interrupt persistent CaDiCaL; completed result is reported even if over budget",
        },
        "replay_seed": replay_seed,
        "seed_witnesses": [{k: v for k, v in seed.items() if k != "raw_safe_region"} for seed in seeds],
        "iterations": [], "result": "INCONCLUSIVE",
    }
    cuts = []
    cut_status = []
    rows = []
    core_sizes_raw = []
    core_sizes_reduced = []
    generated_core_records = []
    candidate_index = 0

    def record_candidate(source, separator, raw_safe, safe, master_time=None, master_stats=None):
        nonlocal candidate_index
        candidate_index += 1
        if time.monotonic() - started >= total_time:
            report["result"] = "GLOBAL_TIMEOUT"
            return True
        remaining = total_time - (time.monotonic() - started)
        result = _temporal_candidate(
            instance, separator, horizon, solver_name,
            min(subproblem_time, max(0.001, remaining)), core_minimize_time,
            mode, subset_scan_max_size, core_restarts, core_restart_stop_size,
            calibration_full_solve, candidate_index,
        )
        result["source"] = source
        result["iteration"] = candidate_index
        result["separator"] = separator
        result["raw_safe_region"] = raw_safe
        result["canonical_safe_region"] = safe
        result["master_solve_time"] = master_time
        result["master_stats"] = master_stats
        result["retry"] = False
        result["master_clauses"] = master_formula["clause_count"]
        if result["result"] == "TIMEOUT":
            if time.monotonic() - started >= total_time:
                report["result"] = "GLOBAL_TIMEOUT"
                report["iterations"].append(result)
                rows.append(_csv_row(result, len(cuts)))
                return True
            remaining = total_time - (time.monotonic() - started)
            result = _temporal_candidate(
                instance, separator, horizon, solver_name,
                min(subproblem_retry_time, max(0.001, remaining)), core_minimize_time,
                mode, subset_scan_max_size, core_restarts, core_restart_stop_size,
                calibration_full_solve, candidate_index,
            )
            result.update(source=source, iteration=candidate_index, separator=separator,
                          raw_safe_region=raw_safe, canonical_safe_region=safe,
                          master_solve_time=master_time, master_stats=master_stats, retry=True,
                          master_clauses=master_formula["clause_count"])
        if result["result"] == "SAT":
            report["result"] = "FOUND_DYNAMIC_SOLUTION"
            report["solution"] = result
            report["iterations"].append(result)
            rows.append(_csv_row(result, len(cuts)))
            return True
        if result["result"] == "TIMEOUT":
            report["result"] = "SUBPROBLEM_UNKNOWN"
            result["cut_added"] = False
            report["iterations"].append(result)
            rows.append(_csv_row(result, len(cuts)))
            return True
        if result["result"] != "UNSAT":
            report["result"] = result["result"]
            report["iterations"].append(result)
            rows.append(_csv_row(result, len(cuts)))
            return True
        core_records = result.get("candidate_cores")
        if not core_records:
            if not result.get("reduced_core"):
                report["result"] = "INVALID_TEMPORAL_CORE"
                report["iterations"].append(result)
                rows.append(_csv_row(result, len(cuts)))
                return True
            core_records = [{
                "source_type": "deletion-core", "restart_index": 0,
                "raw": result.get("raw_core", result["reduced_core"]),
                "reduced": result["reduced_core"],
                "subset_minimal": result.get("subset_minimal", False),
            }]
        core_records = _internal_core_reduce(core_records)
        cuts_added = 0
        redundant = 0
        subsuming_old = 0
        cut_results = []
        for core_record in core_records:
            raw = frozenset(core_record.get("raw", core_record["reduced"]))
            reduced = frozenset(core_record["reduced"])
            if not reduced or not reduced <= set(separator):
                report["result"] = "INVALID_TEMPORAL_CORE"
                result["error"] = f"Invalid semantic core {sorted(reduced)} for R={separator}"
                report["iterations"].append(result)
                rows.append(_csv_row(result, len(cuts)))
                return True
            generated_core_records.append({
                **core_record, "iteration": candidate_index,
                "source": source,
            })
            core_sizes_raw.append(len(raw))
            core_sizes_reduced.append(len(reduced))
            status = "added"
            if any(existing <= reduced for existing in cuts):
                status = "subsumed_by_existing"
                redundant += 1
                added = False
            else:
                for i, existing in enumerate(cuts):
                    if reduced < existing and cut_status[i] == "active":
                        cut_status[i] = "subsumed_by_new"
                        subsuming_old += 1
                cuts.append(reduced)
                cut_status.append("active")
                added = True
                cuts_added += 1
                master_solver.add_clause([-master_formula["r"][v] for v in sorted(reduced)])
                master_formula["clause_count"] += 1
            cut_results.append({
                **core_record, "cut_added": added, "cut_status": status,
                "vertices": sorted(reduced),
                "theoretical_separator_coverage": _coverage(len(reduced), instance.n, defense_budget),
            })
        report["master_encoding"]["clauses"] = master_formula["clause_count"]
        result.update(
            candidate_cores=cut_results,
            raw_core=core_records[0].get("raw"),
            reduced_core=core_records[0].get("reduced"),
            cuts_generated=len(core_records), cuts_added_this_iteration=cuts_added,
            cuts_redundant=redundant, cuts_subsuming_old=subsuming_old,
            cut_added=cuts_added > 0,
            cut_status="added" if cuts_added else "subsumed_by_existing",
            cuts_total=sum(status == "active" for status in cut_status),
            theoretical_separator_coverage=(
                _coverage(len(core_records[0]["reduced"]), instance.n, defense_budget)
                if core_records else None
            ),
        )
        report["iterations"].append(result)
        rows.append(_csv_row(result, len(cuts)))
        rows[-1]["master_clauses"] = master_formula["clause_count"]
        return False

    master_solver = Solver(name=solver_name)
    master_formula = _build_master(instance, defense_budget, master_solver)
    report["master_encoding"] = {
        "semantic_variables": 2 * instance.n,
        "auxiliary_variables": master_formula["manager"].auxiliary,
        "variables": master_formula["manager"].top,
        "base_clauses": master_formula["base_clauses"],
        "clauses": master_formula["base_clauses"],
        "assumptions": master_formula["assumptions"],
    }
    try:
        candidate_seeds = seeds + ([replay_seed] if replay_seed else [])
        for seed in candidate_seeds:
            if time.monotonic() - started >= total_time:
                report["result"] = "GLOBAL_TIMEOUT"
                break
            stop = record_candidate(seed["source"], seed["separator"], seed["raw_safe_region"], seed["safe_region"])
            if stop:
                break
        else:
            for loop_index in range(max_iterations):
                if time.monotonic() - started >= total_time:
                    report["result"] = "GLOBAL_TIMEOUT"
                    break
                before = master_solver.accum_stats()
                remaining = total_time - (time.monotonic() - started)
                master_started = time.monotonic()
                result = master_solver.solve(assumptions=master_formula["assumptions"])
                elapsed = time.monotonic() - master_started
                stats = _stats_delta(master_solver, before)
                master_over_budget = elapsed > min(master_query_time, remaining)
                if master_over_budget:
                    report["master_query_over_budget"] = {
                        "solve_time": elapsed,
                        "budget": min(master_query_time, remaining),
                        "note": "PySAT cannot interrupt this persistent CaDiCaL query; completed SAT/UNSAT result retained",
                    }
                if result is False:
                    report["result"] = f"UNSAT_AT_HORIZON_{horizon}"
                    report["master_unsat_proof"] = {"solve_time": elapsed, "stats": stats,
                                                     "cuts": [sorted(cut) for cut in cuts]}
                    if master_over_budget:
                        report["master_unsat_proof"]["over_budget"] = True
                    break
                separator, raw_safe, safe = _decode_master(master_solver, master_formula, instance, defense_budget)
                stop = record_candidate(f"master:{loop_index + 1}", separator, raw_safe, safe, elapsed, stats)
                if stop:
                    break
            else:
                report["result"] = "ITERATION_LIMIT"
    finally:
        master_formula["r_at_most"].close()
        master_solver.delete()

    report["elapsed"] = time.monotonic() - started
    generated_hist = Counter(len(row["reduced"]) for row in generated_core_records)
    active_cuts = [cut for cut, status in zip(cuts, cut_status) if status == "active"]
    active_hist = Counter(len(cut) for cut in active_cuts)
    all_frequency = Counter(v for row in generated_core_records for v in row["reduced"])
    small_frequency = Counter(v for row in generated_core_records if len(row["reduced"]) <= 3
                              for v in row["reduced"])
    report["summary"] = {
        "iterations": len(report["iterations"]),
        "temporal_sat": sum(row["result"] == "SAT" for row in report["iterations"]),
        "temporal_unsat": sum(row["result"] == "UNSAT" for row in report["iterations"]),
        "temporal_timeout": sum(row["result"] == "TIMEOUT" for row in report["iterations"]),
        "cuts_added": len(cuts),
        "active_cuts": len(active_cuts),
        "all_generated_core_histogram": dict(sorted(generated_hist.items())),
        "active_cut_histogram": dict(sorted(active_hist.items())),
        "top_core_vertices": [
            {"vertex": vertex, "all_core_frequency": count,
             "small_core_frequency": small_frequency[vertex]}
            for vertex, count in all_frequency.most_common(20)
        ],
        "cut_status": cut_status,
        "raw_core_histogram": dict(sorted(Counter(core_sizes_raw).items())),
        "reduced_core_histogram": dict(sorted(Counter(core_sizes_reduced).items())),
        "mean_raw_core_size": sum(core_sizes_raw) / len(core_sizes_raw) if core_sizes_raw else None,
        "mean_reduced_core_size": sum(core_sizes_reduced) / len(core_sizes_reduced) if core_sizes_reduced else None,
        "median_raw_core_size": _median(core_sizes_raw),
        "median_reduced_core_size": _median(core_sizes_reduced),
        "mean_reduction_ratio": (
            sum(r / raw for r, raw in zip(core_sizes_reduced, core_sizes_raw)) / len(core_sizes_raw)
            if core_sizes_raw else None
        ),
        "master_clauses": report["master_encoding"]["clauses"],
    }
    report["cut_sets"] = [sorted(cut) for cut in cuts]
    report["generated_cores"] = generated_core_records
    report["csv_rows"] = rows
    return report


def _median(values):
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    return ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2


def _csv_row(result, cuts_total):
    propagation_scan = result.get("propagation_scan") or {}
    tested = propagation_scan.get("tested", {})
    conflicts = propagation_scan.get("conflicts", {})
    candidate_cores = result.get("candidate_cores") or []
    raw_cores = result.get("restart_cores") or []
    core_sizes = [len(row.get("reduced", [])) for row in candidate_cores]
    minimize_calls = sum(row.get("minimize_calls", 0) for row in raw_cores)
    minimize_time = sum(row.get("minimize_time", 0.0) for row in raw_cores)
    minimize_over = sum(row.get("calls_over_soft_limit", 0) for row in raw_cores)
    coverage = result.get("theoretical_separator_coverage") or {}
    return {
        "iteration": result.get("iteration"), "source": result.get("source"),
        "master_solve_time": result.get("master_solve_time"),
        "separator_size": len(result.get("separator", [])),
        "canonical_safe_size": len(result.get("canonical_safe_region", [])),
        "temporal_result": result.get("result"), "temporal_solve_time": result.get("solve_time"),
        "raw_core_size": len(result.get("raw_core") or []),
        "reduced_core_size": len(result.get("reduced_core") or []),
        "propagation_tested_1": tested.get("1", 0),
        "propagation_tested_2": tested.get("2", 0),
        "propagation_tested_3": tested.get("3", 0),
        "propagation_unsat_1": conflicts.get("1", 0),
        "propagation_unsat_2": conflicts.get("2", 0),
        "propagation_unsat_3": conflicts.get("3", 0),
        "full_temporal_solved": result.get("full_temporal_solved"),
        "restart_attempts": result.get("restart_attempts"),
        "raw_cores_unique": result.get("raw_cores_unique", len(raw_cores)),
        "reduced_cores_unique": result.get("reduced_cores_unique", len(candidate_cores)),
        "smallest_core_size": min(core_sizes) if core_sizes else result.get("smallest_core_size"),
        "core_minimize_calls": minimize_calls or result.get("core_minimize_calls"),
        "core_minimize_time": minimize_time or result.get("core_minimize_time"),
        "core_minimize_calls_over_soft_limit": minimize_over or result.get(
            "core_minimize_calls_over_soft_limit", 0
        ),
        "core_minimize_soft_limit_seconds": result.get("core_minimize_soft_limit_seconds"),
        "hard_interrupt_supported": result.get("hard_interrupt_supported", False),
        "cuts_generated": result.get("cuts_generated", len(candidate_cores)),
        "cuts_added": result.get("cuts_added_this_iteration", int(bool(result.get("cut_added")))),
        "cuts_redundant": result.get("cuts_redundant", 0),
        "cuts_subsuming_old": result.get("cuts_subsuming_old", 0),
        "cut_added": result.get("cut_added"), "cut_status": result.get("cut_status"),
        "theoretical_separator_fraction": coverage.get("fraction"),
        "theoretical_separator_log10_fraction": coverage.get("log10_fraction"),
        "temporal_decisions": result.get("stats", {}).get("decisions"),
        "temporal_conflicts": result.get("stats", {}).get("conflicts"),
        "temporal_propagations": result.get("stats", {}).get("propagations"),
        "temporal_restarts": result.get("stats", {}).get("restarts"),
        "cuts_total": cuts_total, "master_active_cuts": result.get("cuts_total"),
        "master_clauses": result.get("master_clauses"),
    }


def write_benders_outputs(report, out_dir):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    configuration = report.get("configuration", {})
    if configuration.get("mode") == "multi-small-core":
        mode_suffix = f"multi_small_core_s{configuration.get('subset_scan_max_size', 3)}"
    else:
        mode_suffix = "single_core"
    json_path = out_dir / f"separator_benders_pilot_{mode_suffix}.json"
    csv_path = out_dir / f"separator_benders_pilot_{mode_suffix}.csv"
    payload = {key: value for key, value in report.items() if key != "csv_rows"}
    json_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    rows = report.get("csv_rows", [])
    fields = list(rows[0]) if rows else [
        "iteration", "source", "master_solve_time", "separator_size", "canonical_safe_size",
        "temporal_result", "temporal_solve_time", "raw_core_size", "reduced_core_size",
        "propagation_tested_1", "propagation_tested_2", "propagation_tested_3",
        "propagation_unsat_1", "propagation_unsat_2", "propagation_unsat_3",
        "full_temporal_solved", "restart_attempts", "raw_cores_unique", "reduced_cores_unique",
        "smallest_core_size", "core_minimize_calls", "core_minimize_time",
        "core_minimize_calls_over_soft_limit", "core_minimize_soft_limit_seconds",
        "hard_interrupt_supported", "cuts_generated", "cuts_added", "cuts_redundant",
        "cuts_subsuming_old", "cut_added",
        "cut_status", "theoretical_separator_fraction", "theoretical_separator_log10_fraction",
        "temporal_decisions", "temporal_conflicts", "temporal_propagations", "temporal_restarts",
        "cuts_total", "master_active_cuts", "master_clauses",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    return json_path, csv_path
