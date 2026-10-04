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
import time
from collections import Counter
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
                "core_minimize_timeouts": minimize_timeouts,
            }
        finally:
            encoder.close()


def _temporal_candidate_worker(connection, instance, separator, horizon, solver_name, minimize_time):
    try:
        result = _temporal_candidate_impl(
            instance, separator, horizon, solver_name, minimize_time, connection
        )
        connection.send(("RESULT", result))
    except BaseException as exc:
        connection.send(("ERROR", {"result": "ERROR", "error": f"{type(exc).__name__}: {exc}"}))
    finally:
        connection.close()


def _temporal_candidate(instance, separator, horizon, solver_name, timeout, minimize_time):
    """Use a fresh process so a hard timeout cannot corrupt another solver."""
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe(duplex=False)
    process = context.Process(
        target=_temporal_candidate_worker,
        args=(child, instance, list(separator), horizon, solver_name, minimize_time),
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
        # The full assumption set was proved UNSAT before the worker was stopped.
        # Its latest published core remains a sound cut even if minimization was
        # interrupted by the per-candidate wall limit.
        raw = latest_core["raw_core"]
        reduced = latest_core["reduced_core"]
        return {
            **{k: v for k, v in latest_core.items() if k not in {"raw_core", "reduced_core"}},
            "result": "UNSAT", "solve_time": elapsed,
            "raw_core": raw, "reduced_core": reduced, "subset_minimal": False,
            "core_minimize_calls": 0, "core_minimize_time": 0.0,
            "core_minimize_timeouts": 1, "worker_timeout_after_core": True,
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
    seeds = _load_seed_witnesses(seed_witnesses, instance, defense_budget)
    report = {
        "query": {"T": horizon, "K": burned_bound, "defense_budget": defense_budget,
                  "saved_target": saved_target, "firefighters": firefighters},
        "configuration": {"solver": solver_name, "max_iterations": max_iterations,
                          "master_query_time": master_query_time, "subproblem_time": subproblem_time,
                          "subproblem_retry_time": subproblem_retry_time,
                          "core_minimize_time": core_minimize_time, "total_time": total_time,
                          "master_query_timeout_mode": "soft; Cadical300 limited-solve interruption is unsupported",
                          "subproblem_timeout_mode": "hard process timeout",
                          "core_minimize_timeout_mode": "posthoc per-call elapsed-time check; a CaDiCaL call cannot be preempted while retaining its learned state"},
        "backend_capabilities": {
            "assumption_core": True,
            "limited_solve": False if solver_name.startswith("cadical") else "not preflighted",
            "master_wall_timeout": "cannot interrupt persistent CaDiCaL; completed result is reported even if over budget",
        },
        "seed_witnesses": [{k: v for k, v in seed.items() if k != "raw_safe_region"} for seed in seeds],
        "iterations": [], "result": "INCONCLUSIVE",
    }
    cuts = []
    cut_status = []
    rows = []
    core_sizes_raw = []
    core_sizes_reduced = []
    candidate_index = 0

    def record_candidate(source, separator, raw_safe, safe, master_time=None, master_stats=None):
        nonlocal candidate_index
        candidate_index += 1
        if time.monotonic() - started >= total_time:
            report["result"] = "GLOBAL_TIMEOUT"
            return True
        remaining = total_time - (time.monotonic() - started)
        result = _temporal_candidate(instance, separator, horizon, solver_name,
                                    min(subproblem_time, max(0.001, remaining)), core_minimize_time)
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
            result = _temporal_candidate(instance, separator, horizon, solver_name,
                                         min(subproblem_retry_time, max(0.001, remaining)), core_minimize_time)
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
        raw = frozenset(result["raw_core"])
        reduced = frozenset(result["reduced_core"])
        core_sizes_raw.append(len(raw))
        core_sizes_reduced.append(len(reduced))
        status = "added"
        if any(existing <= reduced for existing in cuts):
            status = "subsumed_by_existing"
            added = False
        else:
            for i, existing in enumerate(cuts):
                if reduced < existing:
                    cut_status[i] = "subsumed_by_new"
            cuts.append(reduced)
            cut_status.append("active")
            added = True
            cut_clause = [-master_formula["r"][v] for v in sorted(reduced)]
            master_solver.add_clause(cut_clause)
            master_formula["clause_count"] += 1
            report["master_encoding"]["clauses"] = master_formula["clause_count"]
        result.update(cut_added=added, cut_status=status, cuts_total=len(cuts),
                      theoretical_separator_coverage=_coverage(len(reduced), instance.n, defense_budget))
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
        for seed in seeds:
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
    report["summary"] = {
        "iterations": len(report["iterations"]),
        "temporal_sat": sum(row["result"] == "SAT" for row in report["iterations"]),
        "temporal_unsat": sum(row["result"] == "UNSAT" for row in report["iterations"]),
        "temporal_timeout": sum(row["result"] == "TIMEOUT" for row in report["iterations"]),
        "cuts_added": len(cuts),
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
    report["csv_rows"] = rows
    return report


def _median(values):
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    return ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2


def _csv_row(result, cuts_total):
    return {
        "iteration": result.get("iteration"), "source": result.get("source"),
        "master_solve_time": result.get("master_solve_time"),
        "separator_size": len(result.get("separator", [])),
        "canonical_safe_size": len(result.get("canonical_safe_region", [])),
        "temporal_result": result.get("result"), "temporal_solve_time": result.get("solve_time"),
        "raw_core_size": len(result.get("raw_core") or []),
        "reduced_core_size": len(result.get("reduced_core") or []),
        "core_minimize_calls": result.get("core_minimize_calls"),
        "core_minimize_time": result.get("core_minimize_time"),
        "core_minimize_timeouts": result.get("core_minimize_timeouts"),
        "cut_added": result.get("cut_added"), "cut_status": result.get("cut_status"),
        "theoretical_separator_fraction": result.get("theoretical_separator_coverage", {}).get("fraction"),
        "theoretical_separator_log10_fraction": result.get("theoretical_separator_coverage", {}).get("log10_fraction"),
        "temporal_decisions": result.get("stats", {}).get("decisions"),
        "temporal_conflicts": result.get("stats", {}).get("conflicts"),
        "temporal_propagations": result.get("stats", {}).get("propagations"),
        "temporal_restarts": result.get("stats", {}).get("restarts"),
        "cuts_total": cuts_total, "master_clauses": None,
    }


def write_benders_outputs(report, out_dir):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "separator_benders_pilot.json"
    csv_path = out_dir / "separator_benders_pilot.csv"
    payload = {key: value for key, value in report.items() if key != "csv_rows"}
    json_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    rows = report.get("csv_rows", [])
    fields = list(rows[0]) if rows else [
        "iteration", "source", "master_solve_time", "separator_size", "canonical_safe_size",
        "temporal_result", "temporal_solve_time", "raw_core_size", "reduced_core_size",
        "core_minimize_calls", "core_minimize_time", "core_minimize_timeouts", "cut_added",
        "cut_status", "theoretical_separator_fraction", "theoretical_separator_log10_fraction",
        "temporal_decisions", "temporal_conflicts", "temporal_propagations", "temporal_restarts",
        "cuts_total", "master_clauses",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    return json_path, csv_path
