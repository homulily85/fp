"""Stratified separator Benders diagnostic for one-firefighter targets."""

from __future__ import annotations

import csv
import json
import time
from pathlib import Path

from pysat.solvers import Solver

from .canonical_target import _pair_canonicalization_regression
from .instance import read_instance
from .separator_benders import (
    _build_master,
    _coverage,
    _decode_master,
    _temporal_candidate,
)


def _master_assumptions(formula, safe_required):
    threshold = formula["s_counter"].assumption(safe_required)
    return [*formula["assumptions"][:2], threshold]


def _solve_static_level(solver, formula, instance, defense_count, safe_required, soft_limit):
    assumptions = _master_assumptions(formula, safe_required)
    before = solver.accum_stats()
    started = time.monotonic()
    result = solver.solve(assumptions=assumptions)
    elapsed = time.monotonic() - started
    after = solver.accum_stats()
    stats = {
        key: after.get(key, 0) - before.get(key, 0)
        for key in ("decisions", "conflicts", "propagations", "restarts")
    }
    record = {
        "q": safe_required,
        "result": "SAT" if result else "UNSAT",
        "solve_time": elapsed,
        "over_soft_limit": elapsed > soft_limit,
        "variables": formula["manager"].top,
        "clauses": formula["clause_count"],
        "assumptions": assumptions,
        "stats": stats,
        "canonical_safe_size": None,
    }
    witness = None
    if result:
        separator, raw_safe, safe = _decode_master(solver, formula, instance, defense_count)
        if len(safe) < safe_required:
            raise AssertionError("STATIC_MASTER_MODEL_VALIDATION_FAILED: canonical safe region below q")
        witness = {
            "separator": separator,
            "raw_safe_region": raw_safe,
            "canonical_safe_region": safe,
            "canonical_safe_size": len(safe),
        }
        record["canonical_safe_size"] = len(safe)
    return record, witness


def _find_static_qmax(solver, formula, instance, defense_count, q_min, ceiling, soft_limit, total_deadline):
    queries = []

    def query(q):
        if time.monotonic() >= total_deadline:
            return {"q": q, "result": "UNKNOWN", "reason": "GLOBAL_TIMEOUT"}, None
        record, witness = _solve_static_level(
            solver, formula, instance, defense_count, q, soft_limit
        )
        queries.append(record)
        return record, witness

    lower, lower_witness = query(q_min)
    if lower["result"] != "SAT":
        return {
            "queries": queries, "q_max": None,
            "proved": lower["result"] == "UNSAT",
            "status": "STATIC_TARGET_UNSAT" if lower["result"] == "UNSAT" else "UNKNOWN",
            "witness": None,
        }
    low = q_min
    best_witness = lower_witness
    high = None
    probe = max(q_min + 1, 2 * q_min)
    while low < ceiling:
        probe = min(probe, ceiling)
        record, witness = query(probe)
        if record["result"] == "UNKNOWN":
            return {"queries": queries, "q_max": None, "proved": False,
                    "status": "UNKNOWN", "witness": None}
        if record["result"] == "UNSAT":
            high = probe
            break
        low, best_witness = probe, witness
        if low == ceiling:
            return {"queries": queries, "q_max": low, "proved": True,
                    "status": "PROVED_AT_CEILING", "witness": best_witness}
        probe *= 2

    if high is None:
        return {"queries": queries, "q_max": low, "proved": True,
                "status": "PROVED_AT_CEILING", "witness": best_witness}
    while high - low > 1:
        middle = (low + high) // 2
        record, witness = query(middle)
        if record["result"] == "UNKNOWN":
            return {"queries": queries, "q_max": None, "proved": False,
                    "status": "UNKNOWN", "witness": None}
        if record["result"] == "SAT":
            low, best_witness = middle, witness
        else:
            high = middle
    return {"queries": queries, "q_max": low, "proved": True,
            "status": "BRACKETED", "witness": best_witness}


def _validate_temporal_witness(instance, horizon, defense_count, q, separator, safe, result):
    if sorted(result.get("defended", [])) != sorted(separator):
        raise AssertionError("SAT temporal model's defended set differs from the separator")
    safe_set = set(safe)
    schedule = result.get("schedule")
    if schedule is None:
        raise AssertionError("SAT temporal result is missing its schedule")
    from .simulator import simulate

    solution = simulate(instance, 1, schedule)
    if solution.k != result.get("actual_k") or solution.containment_time > horizon:
        raise AssertionError("Temporal SAT schedule failed simulator validation")
    if len(solution.defended) != defense_count or set(solution.defended) != set(separator):
        raise AssertionError("Decoded schedule does not defend the full separator")
    if safe_set.intersection(solution.burned):
        raise AssertionError("Canonical safe region burned in the validated schedule")
    structural_k = instance.n - defense_count - len(safe)
    if solution.k > structural_k or solution.k > instance.n - defense_count - q:
        raise AssertionError("Actual K exceeds the structural safe-region guarantee")
    return solution


def run_stratified_separator_benders(
    path,
    firefighters=1,
    horizon=9,
    burned_bound=989,
    solver_name="cadical300",
    max_iterations_per_level=256,
    subproblem_time=10.0,
    subproblem_retry_time=60.0,
    core_minimize_soft_limit=2.0,
    static_search_soft_limit=30.0,
    total_time=1800.0,
    run_equivalence_regression=True,
):
    if firefighters != 1:
        raise ValueError("stratified-separator-benders supports only --firefighters 1")
    instance = read_instance(path)
    if not 0 <= burned_bound <= instance.n:
        raise ValueError("K must be between zero and n")
    defense_count = firefighters * horizon
    saved_target = instance.n - burned_bound
    q_min = saved_target - defense_count
    if q_min < 1:
        raise ValueError("The canonical minimum safe count q_min must be at least one")
    ceiling = instance.n - len(instance.initial_fire) - defense_count
    if ceiling < q_min:
        raise ValueError("The static safe-count ceiling is below q_min")

    started = time.monotonic()
    deadline = started + total_time
    equivalence = _pair_canonicalization_regression() if run_equivalence_regression else {
        "passed": False, "skipped": True,
    }
    if run_equivalence_regression and not equivalence.get("passed"):
        raise RuntimeError("CANONICAL_EQUIVALENCE_FAILED")

    solver = Solver(name=solver_name)
    formula = _build_master(instance, defense_count, solver)
    report = {
        "query": {
            "T": horizon, "K": burned_bound, "firefighters": firefighters,
            "defense_count": defense_count, "saved_target": saved_target,
            "q_min": q_min, "safe_count_ceiling": ceiling,
        },
        "configuration": {
            "solver": solver_name,
            "max_iterations_per_level": max_iterations_per_level,
            "subproblem_time": subproblem_time,
            "subproblem_retry_time": subproblem_retry_time,
            "core_minimize_soft_limit_seconds": core_minimize_soft_limit,
            "static_search_soft_limit_seconds": static_search_soft_limit,
            "total_time": total_time,
            "mode": "single-core deletion minimization; no propagation/restarts",
            "static_query_interrupt": "soft; persistent PySAT/CaDiCaL solve cannot be interrupted",
        },
        "canonical_equivalence_regression": equivalence,
        "master_encoding": {
            "semantic_variables": 2 * instance.n,
            "auxiliary_variables": formula["manager"].auxiliary,
            "variables": formula["manager"].top,
            "base_clauses": formula["base_clauses"],
            "clauses": formula["clause_count"],
        },
        "static_search": {}, "levels": [], "iterations": [],
        "cuts": [], "result": "INCONCLUSIVE",
    }

    all_cuts = []
    cut_status = []
    seen_separators = set()
    csv_static = []
    csv_candidates = []
    global_iteration = 0

    def add_cut(vertices):
        nonlocal formula
        candidate = frozenset(vertices)
        if not candidate or len(candidate) > defense_count:
            raise AssertionError("Invalid temporal core for Benders cut")
        if any(existing <= candidate for existing in all_cuts):
            return {"added": False, "status": "subsumed_by_existing"}
        subsumed = 0
        for index, old in enumerate(all_cuts):
            if candidate < old and cut_status[index] == "active":
                cut_status[index] = "subsumed_by_new"
                subsumed += 1
        all_cuts.append(candidate)
        cut_status.append("active")
        solver.add_clause([-formula["r"][v] for v in sorted(candidate)])
        formula["clause_count"] += 1
        report["master_encoding"]["clauses"] = formula["clause_count"]
        return {"added": True, "status": "added", "subsumes_old": subsumed}

    def test_candidate(q, separator, raw_safe, safe, source, level_record):
        nonlocal global_iteration
        global_iteration += 1
        key = tuple(separator)
        if key in seen_separators:
            report["result"] = "DUPLICATE_SEPARATOR_AFTER_CUT"
            return "STOP", {"error": "A previously tested separator reappeared"}
        seen_separators.add(key)
        if len(safe) < q:
            report["result"] = "STATIC_MASTER_MODEL_VALIDATION_FAILED"
            return "STOP", {"error": "Canonical safe region is below the current threshold"}
        higher_closed = bool(level_record.get("previous_higher_level_proved_unsat"))
        if higher_closed and len(safe) != q:
            report["result"] = "STRATIFICATION_INVARIANT_FAILED"
            return "STOP", {"error": f"Expected canonical safe size {q}, got {len(safe)}"}

        primary = _temporal_candidate(
            instance, separator, horizon, solver_name, subproblem_time,
            core_minimize_soft_limit, mode="single-core",
        )
        final = primary
        retry_used = False
        retry = None
        if primary["result"] == "TIMEOUT":
            retry_used = True
            retry = _temporal_candidate(
                instance, separator, horizon, solver_name, subproblem_retry_time,
                core_minimize_soft_limit, mode="single-core",
            )
            final = retry
        result_name = final["result"]
        row = {
            **final,
            "source": source,
            "level_q": q,
            "iteration_in_level": level_record["candidate_count"] + 1,
            "global_iteration": global_iteration,
            "separator": separator,
            "raw_safe_region": raw_safe,
            "canonical_safe_region": safe,
            "canonical_safe_size": len(safe),
            "primary_result": primary["result"],
            "primary_time": primary.get("solve_time"),
            "retry_used": retry_used,
            "retry_result": retry.get("result") if retry else None,
            "retry_time": retry.get("solve_time") if retry else None,
            "final_temporal_result": result_name,
            "cuts_entering_level": level_record["cuts_entering_level"],
            "master_clauses_before_cut": formula["clause_count"],
        }
        level_record["candidate_count"] += 1
        if result_name == "SAT":
            solution = _validate_temporal_witness(
                instance, horizon, defense_count, q, separator, safe, final
            )
            row["actual_k"] = solution.k
            row["containment_time"] = solution.containment_time
            row["structural_k_bound"] = instance.n - defense_count - len(safe)
            row["schedule"] = [list(actions) for actions in solution.schedule]
            row["validated"] = True
            report["iterations"].append(row)
            csv_candidates.append(_candidate_csv_row(row, len(all_cuts), formula["clause_count"]))
            report["solution"] = row
            return "SAT", row
        if result_name != "UNSAT":
            report["iterations"].append(row)
            csv_candidates.append(_candidate_csv_row(row, len(all_cuts), formula["clause_count"]))
            report["result"] = "TEMPORAL_UNKNOWN" if result_name == "TIMEOUT" else result_name
            return "STOP", row

        if final.get("assumption_count") != defense_count:
            raise AssertionError("Temporal subproblem must use exactly one assumption per defended vertex")
        core = final.get("reduced_core")
        if not core or not set(core) <= set(separator):
            report["result"] = "INVALID_TEMPORAL_CORE"
            row["error"] = "Reduced core is not a semantic subset of the candidate separator"
            report["iterations"].append(row)
            csv_candidates.append(_candidate_csv_row(row, len(all_cuts), formula["clause_count"]))
            return "STOP", row
        cut_result = add_cut(core)
        row.update(
            raw_core=final.get("raw_core"), reduced_core=core,
            raw_core_size=len(final.get("raw_core") or []),
            reduced_core_size=len(core),
            core_minimize_calls=final.get("core_minimize_calls"),
            core_minimize_time=final.get("core_minimize_time"),
            core_minimize_soft_limit_seconds=final.get("core_minimize_soft_limit_seconds"),
            core_minimize_calls_over_soft_limit=final.get("core_minimize_calls_over_soft_limit", 0),
            hard_interrupt_supported=final.get("hard_interrupt_supported", False),
            cut_added=cut_result["added"], cut_status=cut_result["status"],
            cut_subsumes_old=cut_result.get("subsumes_old", 0),
            cuts_total=len(all_cuts), active_cuts=sum(status == "active" for status in cut_status),
            theoretical_separator_coverage=_coverage(len(core), instance.n, defense_count),
        )
        level_record["temporal_unsat"] += 1
        if cut_result["added"]:
            level_record["cuts_learned"] += 1
        report["iterations"].append(row)
        csv_candidates.append(_candidate_csv_row(row, len(all_cuts), formula["clause_count"]))
        return "UNSAT", row

    try:
        static_started = time.monotonic()
        static = _find_static_qmax(
            solver, formula, instance, defense_count, q_min, ceiling,
            static_search_soft_limit, deadline,
        )
        report["static_search"] = static
        for query in static["queries"]:
            csv_static.append({
                "q": query["q"], "result": query["result"],
                "solve_time": query.get("solve_time"),
                "variables": query.get("variables"), "clauses": query.get("clauses"),
                "canonical_safe_size": query.get("canonical_safe_size"),
            })
        report["static_search"]["elapsed"] = time.monotonic() - static_started
        report["static_search"]["static_search_soft_limit_seconds"] = static_search_soft_limit
        report["static_search"]["master_interrupt_supported"] = False
        if static["status"] == "UNKNOWN":
            report["result"] = "STATIC_SEARCH_UNKNOWN"
            return _finish_report(report, started, all_cuts, cut_status, seen_separators,
                                  csv_static, csv_candidates)
        if static["status"] == "STATIC_TARGET_UNSAT":
            report["result"] = f"UNSAT_AT_HORIZON_{horizon}"
            report["static_search"]["dynamic_implication"] = "canonical static target is empty"
            return _finish_report(report, started, all_cuts, cut_status, seen_separators,
                                  csv_static, csv_candidates)

        q_max = static["q_max"]
        report["static_search"]["q_max"] = q_max
        report["static_search"]["proved"] = static["proved"]
        witness = static["witness"]
        if not static["proved"] or witness is None:
            report["result"] = "STATIC_SEARCH_UNKNOWN"
            return _finish_report(report, started, all_cuts, cut_status, seen_separators,
                                  csv_static, csv_candidates)

        previous_higher_level_proved_unsat = False
        for q in range(q_max, q_min - 1, -1):
            level = {
                "q": q,
                "status": "IN_PROGRESS",
                "candidate_count": 0,
                "temporal_unsat": 0,
                "temporal_sat": 0,
                "cuts_entering_level": len(all_cuts),
                "cuts_learned": 0,
                "previous_higher_level_proved_unsat": previous_higher_level_proved_unsat,
                "started_elapsed": time.monotonic() - started,
            }
            if time.monotonic() >= deadline:
                level["status"] = "GLOBAL_TIMEOUT"
                report["levels"].append(level)
                report["result"] = "GLOBAL_TIMEOUT"
                break
            if q == q_max:
                outcome, _row = test_candidate(
                    q, witness["separator"], witness["raw_safe_region"],
                    witness["canonical_safe_region"], "phase-a-qmax-witness", level,
                )
                if outcome == "SAT":
                    level["temporal_sat"] += 1
                    level["status"] = "FOUND_DYNAMIC_SOLUTION"
                    level["elapsed"] = time.monotonic() - started - level["started_elapsed"]
                    level["cuts_leaving_level"] = len(all_cuts)
                    report["levels"].append(level)
                    report["result"] = "FOUND_DYNAMIC_SOLUTION"
                    break
                if outcome == "STOP":
                    level["status"] = report["result"]
                    report["levels"].append(level)
                    break
            closed = False
            while level["candidate_count"] < max_iterations_per_level:
                if time.monotonic() >= deadline:
                    level["status"] = "GLOBAL_TIMEOUT"
                    report["levels"].append(level)
                    report["result"] = "GLOBAL_TIMEOUT"
                    closed = True
                    break
                assumptions = _master_assumptions(formula, q)
                before = solver.accum_stats()
                master_started = time.monotonic()
                master_sat = solver.solve(assumptions=assumptions)
                master_elapsed = time.monotonic() - master_started
                master_after = solver.accum_stats()
                master_stats = {
                    key: master_after.get(key, 0) - before.get(key, 0)
                    for key in ("decisions", "conflicts", "propagations", "restarts")
                }
                if master_elapsed > static_search_soft_limit:
                    report.setdefault("master_queries_over_soft_limit", []).append({
                        "q": q, "solve_time": master_elapsed,
                        "budget": static_search_soft_limit,
                        "note": "Persistent CaDiCaL solve cannot be interrupted through the installed PySAT API",
                    })
                if master_sat is False:
                    level["status"] = "MASTER_UNSAT"
                    level["master_unsat"] = {
                        "solve_time": master_elapsed, "stats": master_stats,
                        "assumptions": assumptions,
                    }
                    level["elapsed"] = time.monotonic() - started - level["started_elapsed"]
                    level["cuts_leaving_level"] = len(all_cuts)
                    report["levels"].append(level)
                    previous_higher_level_proved_unsat = True
                    closed = True
                    break
                separator, raw_safe, safe = _decode_master(
                    solver, formula, instance, defense_count
                )
                if len(safe) < q:
                    raise AssertionError("STATIC_MASTER_MODEL_VALIDATION_FAILED")
                outcome, row = test_candidate(
                    q, separator, raw_safe, safe, f"master:q{q}", level,
                )
                if row is not None:
                    row["master_result"] = "SAT"
                    row["master_solve_time"] = master_elapsed
                    row["master_stats"] = master_stats
                    csv_candidates[-1]["master_result"] = "SAT"
                    csv_candidates[-1]["master_solve_time"] = master_elapsed
                    csv_candidates[-1]["master_stats"] = master_stats
                if outcome == "SAT":
                    level["status"] = "FOUND_DYNAMIC_SOLUTION"
                    level["temporal_sat"] += 1
                    level["elapsed"] = time.monotonic() - started - level["started_elapsed"]
                    level["cuts_leaving_level"] = len(all_cuts)
                    report["levels"].append(level)
                    report["result"] = "FOUND_DYNAMIC_SOLUTION"
                    closed = True
                    break
                if outcome == "STOP":
                    level["status"] = report["result"]
                    level["elapsed"] = time.monotonic() - started - level["started_elapsed"]
                    level["cuts_leaving_level"] = len(all_cuts)
                    report["levels"].append(level)
                    closed = True
                    break
            if not closed:
                level["status"] = f"INCONCLUSIVE_AT_LEVEL_{q}"
                level["elapsed"] = time.monotonic() - started - level["started_elapsed"]
                level["cuts_leaving_level"] = len(all_cuts)
                report["levels"].append(level)
                report["result"] = level["status"]
                break
            if report["result"] != "INCONCLUSIVE" or not previous_higher_level_proved_unsat:
                break
        else:
            report["result"] = f"UNSAT_AT_HORIZON_{horizon}"
    finally:
        if formula.get("r_at_most") is not None:
            formula["r_at_most"].close()
        solver.delete()

    return _finish_report(report, started, all_cuts, cut_status, seen_separators,
                          csv_static, csv_candidates)


def _candidate_csv_row(row, cuts_total, clauses):
    return {
        "level_q": row.get("level_q"),
        "iteration_in_level": row.get("iteration_in_level"),
        "global_iteration": row.get("global_iteration"),
        "source": row.get("source"),
        "master_result": row.get("master_result"),
        "master_solve_time": row.get("master_solve_time"),
        "separator_size": len(row.get("separator", [])),
        "canonical_safe_size": row.get("canonical_safe_size"),
        "primary_result": row.get("primary_result"),
        "primary_time": row.get("primary_time"),
        "retry_used": row.get("retry_used"),
        "retry_result": row.get("retry_result"),
        "retry_time": row.get("retry_time"),
        "final_temporal_result": row.get("final_temporal_result"),
        "temporal_decisions": row.get("stats", {}).get("decisions"),
        "temporal_conflicts": row.get("stats", {}).get("conflicts"),
        "temporal_propagations": row.get("stats", {}).get("propagations"),
        "temporal_restarts": row.get("stats", {}).get("restarts"),
        "raw_core_size": row.get("raw_core_size"),
        "reduced_core_size": row.get("reduced_core_size"),
        "core_minimize_calls": row.get("core_minimize_calls"),
        "core_minimize_time": row.get("core_minimize_time"),
        "core_minimize_soft_limit_seconds": row.get("core_minimize_soft_limit_seconds"),
        "core_minimize_calls_over_soft_limit": row.get("core_minimize_calls_over_soft_limit"),
        "cut_added": row.get("cut_added"),
        "cut_redundant": row.get("cut_status") == "subsumed_by_existing",
        "cuts_total": cuts_total,
        "master_clauses": clauses,
        "actual_k": row.get("actual_k"),
    }


def _finish_report(report, started, cuts, cut_status, seen, static_rows, candidate_rows):
    report["elapsed"] = time.monotonic() - started
    report["cuts"] = [
        {"vertices": sorted(cut), "status": status}
        for cut, status in zip(cuts, cut_status)
    ]
    from collections import Counter

    core_histogram = Counter(
        len(row["reduced_core"])
        for row in report.get("iterations", [])
        if row.get("reduced_core")
    )
    report["summary"] = {
        "levels": len(report.get("levels", [])),
        "candidates": len(report.get("iterations", [])),
        "temporal_sat": sum(row.get("final_temporal_result") == "SAT"
                             for row in report.get("iterations", [])),
        "temporal_unsat": sum(row.get("final_temporal_result") == "UNSAT"
                               for row in report.get("iterations", [])),
        "temporal_unknown": sum(row.get("final_temporal_result") == "TIMEOUT"
                                 for row in report.get("iterations", [])),
        "unique_separators": len(seen),
        "cuts_total": len(cuts),
        "active_cuts": sum(status == "active" for status in cut_status),
        "core_histogram": dict(sorted(core_histogram.items())),
    }
    report["csv_static_rows"] = static_rows
    report["csv_candidate_rows"] = candidate_rows
    return report


def write_stratified_outputs(report, out_dir):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "stratified_separator_benders.json"
    static_path = out_dir / "stratified_separator_benders_static.csv"
    candidates_path = out_dir / "stratified_separator_benders_candidates.csv"
    payload = {key: value for key, value in report.items()
               if key not in {"csv_static_rows", "csv_candidate_rows"}}
    json_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    static_fields = ["q", "result", "solve_time", "variables", "clauses", "canonical_safe_size"]
    with static_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=static_fields)
        writer.writeheader()
        writer.writerows(report.get("csv_static_rows", []))
    candidate_rows = report.get("csv_candidate_rows", [])
    candidate_fields = list(candidate_rows[0]) if candidate_rows else [
        "level_q", "iteration_in_level", "global_iteration", "master_result",
        "master_solve_time", "separator_size", "canonical_safe_size", "primary_result",
        "primary_time", "retry_used", "retry_result", "retry_time", "final_temporal_result",
        "raw_core_size", "reduced_core_size", "core_minimize_calls", "core_minimize_time",
        "core_minimize_soft_limit_seconds", "core_minimize_calls_over_soft_limit",
        "temporal_decisions", "temporal_conflicts", "temporal_propagations", "temporal_restarts",
        "cut_added", "cut_redundant", "cuts_total", "master_clauses", "actual_k",
    ]
    with candidates_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=candidate_fields)
        writer.writeheader()
        writer.writerows(candidate_rows)
    return json_path, static_path, candidates_path
