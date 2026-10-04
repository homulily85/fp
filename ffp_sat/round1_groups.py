"""Persistent round-one group decomposition diagnostic."""

from __future__ import annotations

import csv
import json
import multiprocessing
import random
import time
from pathlib import Path

from pysat.solvers import Solver

from .early_exact import (
    append_early_exact_actions,
    run_containment_query,
    run_small_graph_equivalence_regression,
)
from .encoder import Encoder
from .heuristic import threat
from .instance import read_instance
from .preprocess import preprocess
from .simulator import simulate


def modulo_partition(ranked_vertices, group_count):
    """Return the exact rank-modulo partition, preserving rank order."""
    groups = {group_id: [] for group_id in range(group_count)}
    for rank, vertex in enumerate(ranked_vertices):
        groups[rank % group_count].append(vertex)
    return groups


def assert_partition(groups, vertices):
    flattened = [vertex for group in groups.values() for vertex in group]
    if len(flattened) != len(vertices) or set(flattened) != set(vertices):
        raise AssertionError("Groups are not a disjoint exhaustive partition")


def assert_nested(parent, children):
    flattened = children[0] + children[1]
    if len(flattened) != len(parent) or set(flattened) != set(parent):
        raise AssertionError("Modulo-refined children do not exactly cover their parent")


def _stats_delta(solver, before):
    after = solver.accum_stats()
    return {
        key: after.get(key, 0) - before.get(key, 0)
        for key in ("decisions", "conflicts", "propagations", "restarts")
    }


def _limited_preflight(instance, distance, solver_name, horizon, bound, early_rounds=5):
    """Verify UNKNOWN followed by valid same-solver calls on the production-sized formula."""
    solver = Solver(name=solver_name)
    encoder = None
    try:
        encoder = Encoder(instance, 1, solver, distance)
        encoder.ensure_horizon(horizon)
        query = encoder.assumptions(horizon, bound, instance.n)
        append_early_exact_actions(encoder, solver, early_rounds)
        before = solver.accum_stats()
        solver.conf_budget(1)
        tiny = solver.solve_limited(assumptions=query)
        tiny_stats = _stats_delta(solver, before)
        if tiny is not None:
            return {
                "limited_solve_supported": False,
                "reason": "Tiny conflict budget did not produce UNKNOWN",
                "tiny_budget_result": tiny,
                "tiny_budget_stats": tiny_stats,
                "solver_usable_after_unknown": False,
            }

        prefix = [
            encoder.a[vertex, round_number]
            for round_number, vertex in enumerate((568, 546, 599, 67, 732), start=1)
        ]
        before = solver.accum_stats()
        solver.conf_budget(100_000)
        after_unknown = solver.solve_limited(assumptions=query + prefix)
        after_unknown_stats = _stats_delta(solver, before)
        before = solver.accum_stats()
        solver.conf_budget(100_000)
        repeated = solver.solve_limited(assumptions=query + prefix)
        repeat_stats = _stats_delta(solver, before)
        usable = after_unknown is False and repeated is False
        return {
            "limited_solve_supported": usable,
            "unknown_return_value": tiny,
            "tiny_budget_stats": tiny_stats,
            "solver_usable_after_unknown": usable,
            "after_unknown_result": after_unknown,
            "after_unknown_stats": after_unknown_stats,
            "repeat_result": repeated,
            "repeat_stats": repeat_stats,
            "budget_semantics": "conflict cap reset for each solve_limited call; UNKNOWN can be followed by valid calls",
        }
    finally:
        if encoder is not None:
            encoder.close()
        solver.delete()


def _rank_vertices(instance, pool_size, seed):
    rng = random.Random(seed)
    first_action_frequency = [0] * instance.n
    schedules = []
    degree_solution = threat(instance, 1, mode="degree")
    schedules.append(degree_solution.schedule)
    for _ in range(max(0, pool_size - 1)):
        schedules.append(threat(instance, 1, mode="random", rng=rng).schedule)
    for schedule in schedules:
        if schedule and schedule[0]:
            first_action_frequency[schedule[0][0]] += 1
    ranked = sorted(
        range(instance.n),
        key=lambda vertex: (
            -first_action_frequency[vertex],
            -len(instance.adjacency[vertex]),
            vertex,
        ),
    )
    return ranked, {
        "vertex_count": instance.n,
        "policy": "first-action-frequency,degree,vertex-id",
        "pool_size": len(schedules),
        "seed": seed,
        "first_action_frequency": {
            str(vertex): count for vertex, count in enumerate(first_action_frequency) if count
        },
    }


def _timeboxed_limited_solve(solver, assumptions, timeout, conflict_budget=250_000):
    """Run one persistent call so CaDiCaL's search trajectory is not restarted per chunk."""
    started = time.monotonic()
    before_all = solver.accum_stats()
    solver.conf_budget(conflict_budget)
    result = solver.solve_limited(assumptions=assumptions)
    elapsed = time.monotonic() - started
    if elapsed >= timeout:
        result = None
    return result, elapsed, _stats_delta(solver, before_all), 1


def _stage_model(solver, encoder, instance, firefighters, horizon, bound, result):
    if result is not True:
        return None
    model = solver.get_model()
    schedule = [list(actions) for actions in encoder.decode(model, horizon)]
    solution = simulate(instance, firefighters, schedule)
    if solution.k > bound or solution.containment_time > horizon:
        raise AssertionError("SAT model failed simulator validation")
    return {
        "actual_k": solution.k,
        "containment_time": solution.containment_time,
        "schedule": [list(actions) for actions in solution.schedule],
    }


def _rank_group_nodes(ranked, count, parent=None):
    rank_by_vertex = {vertex: rank for rank, vertex in enumerate(ranked)}
    parent_group = None
    if parent is not None:
        parent_group = parent["group_id"]
        parent_level = parent["group_count"]
        child_count = parent_level * 2
        child_ids = (parent_group, parent_group + parent_level)
        children = []
        for child_id in child_ids:
            members = [v for v in ranked if rank_by_vertex[v] % child_count == child_id]
            children.append(members)
        assert_nested(parent["vertices"], children)
        return [
            {
                "group_count": child_count,
                "group_id": child_id,
                "vertices": members,
                "parent_group_id": parent_group,
            }
            for child_id, members in zip(child_ids, children)
        ]
    groups = modulo_partition(ranked, count)
    assert_partition(groups, ranked)
    return [
        {"group_count": count, "group_id": group_id, "vertices": members, "parent_group_id": None}
        for group_id, members in groups.items()
    ]


def _coverage_snapshot(report, max_level):
    """Reconstruct closed and unresolved round-one choices from partial probe history."""
    ranked = report.get("ranking", {}).get("ranked_vertices", [])
    if not ranked:
        return [], []
    rows = {(row["level"], row["group_id"]): row for row in report.get("probes", [])}
    unresolved = set()
    eliminated = set()

    def visit(group_count, group_id):
        vertices = {vertex for rank, vertex in enumerate(ranked) if rank % group_count == group_id}
        row = rows.get((group_count, group_id))
        if row is None:
            unresolved.update(vertices)
            return
        if row["result"] == "UNSAT":
            eliminated.update(vertices)
            return
        if row["result"] == "SAT":
            return
        if group_count >= max_level:
            unresolved.update(vertices)
            return
        visit(group_count * 2, group_id)
        visit(group_count * 2, group_id + group_count)

    for group_id in range(8):
        visit(8, group_id)
    return sorted(eliminated), sorted(unresolved)


def _worker(
    connection,
    path,
    firefighters,
    solver_name,
    horizon,
    bound,
    replay_bounds,
    replay_timeout,
    group_warmup_time,
    warm_root_time,
    levels,
    conflict_budgets,
    ranking_pool_size,
    seed,
):
    solver = encoder = None
    report = {
        "experiment": "round1_group_decomposition",
        "query": {"T": horizon, "K": bound},
        "result": "ERROR",
        "preflight": {},
        "containment_search": {"queries": [], "min_containment_time": None, "proved": False},
        "small_graph_equivalence": {},
        "replay": {"stages": [], "history_match": False},
        "early_exact": {},
        "ranking": {},
        "levels": [],
        "probes": [],
        "remaining_round1_vertices": [],
        "search_state_reused": True,
    }
    try:
        report["result"] = "RUNNING"
        instance = read_instance(path)
        distance, _ = preprocess(instance, firefighters)
        report["preflight"] = _limited_preflight(instance, distance, solver_name, horizon, bound)
        if not report["preflight"].get("limited_solve_supported"):
            report["result"] = "LIMITED_SOLVE_UNSUPPORTED"
            connection.send(report)
            return
        report["small_graph_equivalence"] = run_small_graph_equivalence_regression()

        for query_horizon in (5, 4):
            query = run_containment_query(path, firefighters, solver_name, query_horizon, timeout=120.0)
            report["containment_search"]["queries"].append(query)
            connection.send(report)
            if query["result"] == "TIMEOUT":
                report["result"] = "CONTAINMENT_MIN_UNKNOWN"
                connection.send(report)
                return
            expected = "SAT" if query_horizon == 5 else "UNSAT"
            if query["result"] != expected:
                report["result"] = "CONTAINMENT_PREREQUISITE_FAILED"
                connection.send(report)
                return
        report["containment_search"].update(min_containment_time=5, proved=True)
        connection.send(report)

        solver = Solver(name=solver_name)
        encoder = Encoder(instance, firefighters, solver, distance)
        encoder.ensure_horizon(horizon)
        for replay_bound in replay_bounds:
            assumptions = encoder.assumptions(horizon, replay_bound, instance.n)
            started = time.monotonic()
            result, solve_time, stats, calls = _timeboxed_limited_solve(solver, assumptions, replay_timeout)
            stage = {
                "stage": "replay",
                "K": replay_bound,
                "result": "SAT" if result is True else "UNSAT" if result is False else "TIMEOUT",
                "solve_time": solve_time,
                "stats": stats,
                "limited_calls": calls,
                "assumptions": assumptions,
                "variables": encoder.vars.top,
                "clauses": encoder.clauses,
            }
            stage.update(
                _stage_model(solver, encoder, instance, firefighters, horizon, replay_bound, result) or {}
            )
            report["replay"]["stages"].append(stage)
            connection.send(report)
            if result is not True:
                report["result"] = "REPLAY_TIMEOUT" if result is None else "REPLAY_UNSAT"
                connection.send(report)
                return

        assumptions = encoder.assumptions(horizon, bound, instance.n)
        checkpoint = {
            "variables": encoder.vars.top,
            "clauses": encoder.clauses,
            "assumptions": list(assumptions),
        }
        report["replay"]["final_base_checkpoint"] = checkpoint
        report["replay"]["history_match"] = len(report["replay"]["stages"]) == len(replay_bounds)
        # A replay model should be deterministic for this backend and identical fresh setup.
        if not report["replay"]["history_match"]:
            report["result"] = "HISTORY_MISMATCH"
            connection.send(report)
            return

        early = append_early_exact_actions(encoder, solver, 5, "through")
        if early["variables_added"] != 0 or early["clauses_added"] != 5:
            raise AssertionError(f"Expected five EARLY-EXACT clauses and no vars, got {early}")
        report["early_exact"] = {
            **early,
            "clauses_before": checkpoint["clauses"],
            "clauses_after": encoder.clauses,
            "variables_before": checkpoint["variables"],
            "variables_after": encoder.vars.top,
        }
        ranked, ranking_report = _rank_vertices(instance, ranking_pool_size, seed)
        report["ranking"] = ranking_report
        report["ranking"]["ranked_vertices"] = ranked
        report["group_config"] = {"levels": levels, "conflict_budgets": conflict_budgets}
        report["result"] = "RUNNING"
        connection.send(report)

        selector_by_node = {}
        all_selectors = []
        stats_before_warmup = solver.accum_stats()
        group_warmup_started = time.monotonic()
        unresolved = _rank_group_nodes(ranked, levels[0])
        all_roots_closed = False
        soft_deadline_reached = False
        for level_index, group_count in enumerate(levels):
            budget = conflict_budgets[level_index]
            level_nodes = [node for node in unresolved if node["group_count"] == group_count]
            if not level_nodes:
                break
            level_record = {
                "group_count": group_count,
                "conflict_budget": budget,
                "probed": 0,
                "unsat": 0,
                "sat": 0,
                "unknown": 0,
                "closed_by_children": 0,
            }
            next_nodes = []
            for node_index, node in enumerate(level_nodes):
                if time.monotonic() - group_warmup_started >= group_warmup_time:
                    soft_deadline_reached = True
                    next_nodes.extend(level_nodes[node_index:])
                    break
                selector = encoder.vars.new_aux(f"round1_group[{group_count},{node['group_id']}]")
                selector_by_node[(group_count, node["group_id"])] = selector
                all_selectors.append(selector)
                action_lits = [encoder.a[vertex, 1] for vertex in node["vertices"]]
                encoder.add([-selector, *action_lits])
                assumptions = list(checkpoint["assumptions"])
                assumptions.append(selector)
                assumptions.extend(-other for other in all_selectors if other != selector)
                before = solver.accum_stats()
                started = time.monotonic()
                solver.conf_budget(budget)
                result = solver.solve_limited(assumptions=assumptions)
                solve_time = time.monotonic() - started
                stats = _stats_delta(solver, before)
                status = "SAT" if result is True else "UNSAT" if result is False else "UNKNOWN_BUDGET"
                row = {
                    "level": group_count,
                    "group_id": node["group_id"],
                    "parent_group_id": node["parent_group_id"],
                    "group_size": len(node["vertices"]),
                    "vertices": node["vertices"],
                    "result": status,
                    "solve_time": solve_time,
                    "stats": stats,
                    "selector": selector,
                    "assumptions_count": len(assumptions),
                    "vertices_eliminated": len(node["vertices"]) if result is False else 0,
                }
                level_record["probed"] += 1
                if result is False:
                    level_record["unsat"] += 1
                    node["status"] = "CLOSED_UNSAT"
                elif result is None:
                    level_record["unknown"] += 1
                    node["status"] = "UNKNOWN_BUDGET"
                    if level_index + 1 < len(levels):
                        next_nodes.extend(_rank_group_nodes(ranked, group_count * 2, node))
                    else:
                        node["leaf_unresolved"] = True
                        next_nodes.append(node)
                else:
                    level_record["sat"] += 1
                    schedule = [list(actions) for actions in encoder.decode(solver.get_model(), horizon)]
                    solution = simulate(instance, firefighters, schedule)
                    if solution.k > bound or solution.containment_time > horizon:
                        raise AssertionError("Round-one group SAT model failed simulator validation")
                    if not schedule[0] or schedule[0][0] not in node["vertices"]:
                        raise AssertionError("SAT model's first action is outside the active group")
                    for round_number in range(1, 6):
                        if len(schedule[round_number - 1]) != 1:
                            raise AssertionError("EARLY-EXACT model omitted a required early action")
                    row.update(
                        actual_k=solution.k,
                        containment_time=solution.containment_time,
                        schedule=[list(actions) for actions in solution.schedule],
                    )
                    report["solution"] = row
                    report["probes"].append(row)
                    report["levels"].append(level_record)
                    report["result"] = "SAT_FOUND"
                    connection.send(report)
                    return
                report["probes"].append(row)
                connection.send(report)
            report["levels"].append(level_record)
            unresolved = next_nodes
            eliminated, remaining = _coverage_snapshot(report, levels[-1])
            report["proved_unsat_vertices"] = eliminated
            report["remaining_round1_vertices"] = remaining
            if soft_deadline_reached:
                break
            if not unresolved:
                all_roots_closed = True
                break

        eliminated, remaining = _coverage_snapshot(report, levels[-1])
        report["remaining_round1_vertices"] = remaining
        report["proved_unsat_vertices"] = eliminated
        warmup_stats = _stats_delta(solver, stats_before_warmup)
        report["warmup"] = {
            "wall_time": time.monotonic() - group_warmup_started,
            "group_queries": len(report["probes"]),
            "completed_levels": [row["group_count"] for row in report["levels"]],
            "decisions": warmup_stats["decisions"],
            "conflicts": warmup_stats["conflicts"],
            "propagations": warmup_stats["propagations"],
            "restarts": warmup_stats["restarts"],
            "unresolved_round1_vertices": len(remaining),
            "soft_deadline_reached": soft_deadline_reached,
            "group_selectors": len(all_selectors),
        }
        if all_roots_closed and not remaining:
            report["result"] = "UNSAT_AT_HORIZON_9"
            connection.send(report)
            return

        root_assumptions = list(checkpoint["assumptions"]) + [-selector for selector in all_selectors]
        selector_clause_count = len(all_selectors)
        expected_clauses = checkpoint["clauses"] + early["clauses_added"] + selector_clause_count
        expected_variables = checkpoint["variables"] + selector_clause_count
        if encoder.clauses != expected_clauses or encoder.vars.top != expected_variables:
            raise AssertionError(
                "Warm-root formulation contains unexpected clauses or variables: "
                f"clauses={encoder.clauses}/{expected_clauses}, "
                f"vars={encoder.vars.top}/{expected_variables}"
            )
        if any(literal > 0 for literal in root_assumptions[len(checkpoint["assumptions"]) :]):
            raise AssertionError("A group selector is active in the warm-root query")
        report["warm_root"] = {
            "result": "RUNNING",
            "assumptions": root_assumptions,
            "active_group_selectors": 0,
            "disabled_group_selectors": selector_clause_count,
            "formulation_equivalent_to_early_exact": True,
            "base_variables": checkpoint["variables"],
            "base_clauses": checkpoint["clauses"],
            "early_exact_clauses": early["clauses_added"],
            "selector_definition_clauses": selector_clause_count,
            "variables_before_root": encoder.vars.top,
            "clauses_before_root": encoder.clauses,
            "timeout_seconds": warm_root_time,
        }
        report["result"] = "WARM_ROOT_RUNNING"
        connection.send(report)
        before_root = solver.accum_stats()
        root_started = time.monotonic()
        root_result = solver.solve(assumptions=root_assumptions)
        root_elapsed = time.monotonic() - root_started
        root_stats = _stats_delta(solver, before_root)
        root_report = {
            **report["warm_root"],
            "result": "SAT" if root_result else "UNSAT",
            "solve_time": root_elapsed,
            "stats": root_stats,
        }
        if root_result:
            model_info = _stage_model(solver, encoder, instance, firefighters, horizon, bound, True)
            root_report.update(model_info)
            report["solution"] = model_info
        report["warm_root"] = root_report
        report["result"] = "SAT_FOUND" if root_result else "UNSAT_AT_HORIZON_9"
        connection.send(report)
    except Exception as exc:
        report["result"] = "ERROR"
        report["error"] = f"{type(exc).__name__}: {exc}"
        connection.send(report)
    finally:
        if encoder is not None:
            encoder.close()
        if solver is not None:
            solver.delete()
        connection.close()


def run_round1_group_decomposition(
    path,
    firefighters,
    solver_name,
    horizon,
    bound,
    replay_bounds=(991, 990),
    replay_timeout=90.0,
    group_warmup_time=1800.0,
    warm_root_time=600.0,
    levels=(8, 16, 32, 64),
    conflict_budgets=(100_000, 75_000, 50_000, 30_000),
    total_time=2600.0,
    ranking_pool_size=128,
    seed=0,
):
    """Run one persistent limited-solving decomposition worker with hard global timeout."""
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(
        target=_worker,
        args=(
            sender,
            str(path),
            firefighters,
            solver_name,
            horizon,
            bound,
            tuple(replay_bounds),
            replay_timeout,
            group_warmup_time,
            warm_root_time,
            tuple(levels),
            tuple(conflict_budgets),
            ranking_pool_size,
            seed,
        ),
    )
    latest = {"experiment": "round1_group_decomposition", "result": "ERROR"}
    started = time.monotonic()
    warm_root_started = None
    process.start()
    sender.close()
    timed_out = False
    try:
        while process.is_alive():
            remaining = total_time - (time.monotonic() - started)
            if remaining <= 0:
                timed_out = True
                break
            if warm_root_started is not None and time.monotonic() - warm_root_started >= warm_root_time:
                timed_out = True
                latest["result"] = "WARM_ROOT_TIMEOUT"
                latest.setdefault("warm_root", {})["result"] = "TIMEOUT"
                latest["warm_root"]["solve_time"] = warm_root_time
                latest["warm_root"]["timeout_seconds"] = warm_root_time
                break
            if not receiver.poll(min(0.1, remaining)):
                continue
            try:
                latest = receiver.recv()
            except (EOFError, OSError):
                break
            if latest.get("result") == "WARM_ROOT_RUNNING" and warm_root_started is None:
                warm_root_started = time.monotonic()
            print(
                f"round1 groups: {latest.get('result')} levels="
                f"{[(row['group_count'], row['unsat'], row['sat'], row['unknown']) for row in latest.get('levels', [])]} "
                f"remaining={len(latest.get('remaining_round1_vertices', []))}",
                flush=True,
            )
            if warm_root_started is not None and time.monotonic() - warm_root_started >= warm_root_time:
                timed_out = True
                latest["result"] = "WARM_ROOT_TIMEOUT"
                latest.setdefault("warm_root", {})["result"] = "TIMEOUT"
                latest["warm_root"]["solve_time"] = warm_root_time
                latest["warm_root"]["timeout_seconds"] = warm_root_time
                break
        if timed_out:
            process.terminate()
            process.join(0.5)
            if process.is_alive():
                process.kill()
                process.join(0.5)
            if latest.get("result") != "WARM_ROOT_TIMEOUT":
                latest["result"] = "GLOBAL_TIMEOUT"
            eliminated, unresolved = _coverage_snapshot(
                latest, latest.get("group_config", {}).get("levels", [8, 16, 32, 64])[-1]
            )
            latest["proved_unsat_vertices"] = eliminated
            latest["remaining_round1_vertices"] = unresolved
            probe_rows = latest.get("probes", [])
            level_counts = latest.get("group_config", {}).get("levels", [8, 16, 32, 64])
            latest["partial_levels"] = [
                {
                    "group_count": count,
                    "probed": sum(row["level"] == count for row in probe_rows),
                    "unsat": sum(row["level"] == count and row["result"] == "UNSAT" for row in probe_rows),
                    "sat": sum(row["level"] == count and row["result"] == "SAT" for row in probe_rows),
                    "unknown": sum(
                        row["level"] == count and row["result"] == "UNKNOWN_BUDGET" for row in probe_rows
                    ),
                }
                for count in level_counts
                if not any(level.get("group_count") == count for level in latest.get("levels", []))
            ]
        else:
            process.join(0.5)
            while receiver.poll():
                try:
                    latest = receiver.recv()
                except (EOFError, OSError):
                    break
            if process.exitcode not in (0, None) and latest.get("result") == "RUNNING":
                latest.update(result="WORKER_EXIT", error=f"Worker exited with code {process.exitcode}")
        latest["elapsed_total"] = time.monotonic() - started
        latest["global_time_limit"] = total_time
        return latest
    finally:
        if process.is_alive():
            process.kill()
            process.join(0.5)
        receiver.close()
        process.close()


def write_round1_outputs(report, out_dir, stem="round1_group_decomposition"):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"{stem}.json"
    json_path.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    csv_path = out_dir / f"{stem}.csv"
    fields = (
        "level",
        "group_id",
        "parent_group_id",
        "group_size",
        "result",
        "solve_time",
        "decisions",
        "conflicts",
        "propagations",
        "restarts",
        "vertices_eliminated",
        "remaining_round1_vertices",
        "actual_k",
        "containment_time",
    )
    with csv_path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in report.get("probes", []):
            writer.writerow({**row, **row.get("stats", {})})
    return json_path, csv_path
