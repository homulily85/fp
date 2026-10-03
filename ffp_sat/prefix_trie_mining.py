"""Trie-guided mining of short UNSAT action prefixes."""

from __future__ import annotations

import json
import multiprocessing
import time
from dataclasses import dataclass, field
from pathlib import Path

from pysat.solvers import Solver

from .encoder import Encoder
from .instance import read_instance
from .prefix_core_mining import (
    _schedule_records,
    _stats,
    build_prefix_pool,
    run_master_replay,
)
from .preprocess import preprocess
from .simulator import simulate

UNSAT_RESULTS = {"UNSAT_PROPAGATION", "UNSAT_SOLVE"}


@dataclass
class PrefixTrieNode:
    depth: int
    prefix: tuple[tuple[int, int], ...]
    children: dict[int, PrefixTrieNode] = field(default_factory=dict)
    source_count: int = 0
    best_source_k: int | None = None
    result: str | None = None
    probe_index: int | None = None
    promotion_result: str | None = None


def build_prefix_trie(prefix_rows, prefix_depth):
    root = PrefixTrieNode(depth=0, prefix=())
    for row in prefix_rows:
        pairs = tuple((int(round_number), int(vertex)) for round_number, vertex in row["prefix"])
        if len(pairs) > prefix_depth:
            raise ValueError("Prefix row exceeds requested trie depth")
        if any(round_number != index for index, (round_number, _) in enumerate(pairs, start=1)):
            raise ValueError("Trie mining requires one action in every prefix round")
        node = root
        source_k = int(row["source_k"])
        for round_number, vertex in pairs:
            node.source_count += 1
            node.best_source_k = (
                source_k if node.best_source_k is None else min(node.best_source_k, source_k)
            )
            node = node.children.setdefault(
                vertex,
                PrefixTrieNode(depth=round_number, prefix=(*node.prefix, (round_number, vertex))),
            )
        node.source_count += 1
        node.best_source_k = (
            source_k if node.best_source_k is None else min(node.best_source_k, source_k)
        )
    return root


def ordered_children(node):
    return sorted(
        node.children.items(),
        key=lambda item: (
            -item[1].source_count,
            item[1].best_source_k if item[1].best_source_k is not None else float("inf"),
            item[0],
        ),
    )


def _node_count(node):
    return 1 + sum(_node_count(child) for child in node.children.values())


def _prefix_probe_worker(connection, path, firefighters, solver_name, horizon, bound, prefix):
    solver = encoder = None
    result = {"T": horizon, "K": bound, "prefix": [list(item) for item in prefix], "result": "ERROR"}
    try:
        instance = read_instance(path)
        distance, _ = preprocess(instance, firefighters)
        solver = Solver(name=solver_name)
        if not hasattr(solver, "propagate"):
            raise RuntimeError(f"Solver backend {solver_name!r} does not support propagate()")
        encoder = Encoder(instance, firefighters, solver, distance)
        encoding_started = time.monotonic()
        encoder.ensure_horizon(horizon)
        query_assumptions = encoder.assumptions(horizon, bound, instance.n)
        for literal in query_assumptions:
            solver.add_clause([literal])
        prefix_literals = [encoder.a[vertex, round_number] for round_number, vertex in prefix]
        result.update(
            query_assumptions=query_assumptions,
            prefix_literals=prefix_literals,
            variables=encoder.vars.top,
            clauses=encoder.clauses + len(query_assumptions),
            encoding_time=time.monotonic() - encoding_started,
        )
        connection.send(("READY", result))
        propagation_started = time.monotonic()
        propagation_result = solver.propagate(assumptions=prefix_literals)
        propagation_time = time.monotonic() - propagation_started
        if (
            not isinstance(propagation_result, tuple)
            or len(propagation_result) != 2
            or not isinstance(propagation_result[0], bool)
        ):
            raise RuntimeError("Solver propagate() returned an unsupported result shape")
        consistent, propagated = propagation_result
        result.update(
            propagation_time=propagation_time,
            propagated_literal_count=len(propagated),
            propagation_consistent=consistent,
        )
        if not consistent:
            result["result"] = "UNSAT_PROPAGATION"
            result["solve_time"] = 0.0
            result["stats"] = {"decisions": 0, "conflicts": 0, "propagations": 0, "restarts": 0}
        else:
            connection.send(("SOLVE_STARTED", result))
            before = solver.accum_stats()
            solve_started = time.monotonic()
            satisfiable = solver.solve(assumptions=prefix_literals)
            solve_time = time.monotonic() - solve_started
            result.update(
                result="SAT" if satisfiable else "UNSAT_SOLVE",
                solve_time=solve_time,
                stats=_stats(solver, before),
            )
            if satisfiable:
                model = set(solver.get_model())
                schedule = [list(actions) for actions in encoder.decode(model, horizon)]
                solution = simulate(instance, firefighters, schedule)
                if solution.k > bound or solution.containment_time > horizon:
                    raise AssertionError("SAT trie probe failed simulator validation")
                result.update(
                    actual_k=solution.k,
                    containment_time=solution.containment_time,
                    schedule=[list(actions) for actions in solution.schedule],
                )
        connection.send(("RESULT", result))
    except Exception as exc:
        result.update(result="ERROR", error=f"{type(exc).__name__}: {exc}")
        connection.send(("RESULT", result))
    finally:
        if encoder is not None:
            encoder.close()
        if solver is not None:
            solver.delete()
        connection.close()


def run_trie_probe(path, firefighters, solver_name, horizon, bound, prefix, timeout, setup_timeout=60.0):
    """Probe one prefix in a fresh worker; budget begins just before propagation."""
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(
        target=_prefix_probe_worker,
        args=(sender, str(path), firefighters, solver_name, horizon, bound, prefix),
    )
    started = time.monotonic()
    process.start()
    sender.close()
    latest = {"prefix": [list(item) for item in prefix], "result": "ERROR"}
    ready_time = None
    received_result = False
    timed_out = False
    try:
        while process.is_alive():
            now = time.monotonic()
            if ready_time is None and now - started >= setup_timeout:
                timed_out = True
                break
            if ready_time is not None and now - ready_time >= timeout:
                timed_out = True
                break
            wait = 0.05
            if ready_time is None:
                wait = min(wait, max(0.0, setup_timeout - (now - started)))
            else:
                wait = min(wait, max(0.0, timeout - (now - ready_time)))
            if not receiver.poll(wait):
                continue
            try:
                kind, payload = receiver.recv()
            except (EOFError, OSError):
                break
            if kind == "READY":
                latest = payload
                ready_time = time.monotonic()
            elif kind == "SOLVE_STARTED":
                latest = payload
                # Propagation and solve share one query budget.
            elif kind == "RESULT":
                latest = payload
                received_result = True
        if timed_out:
            process.terminate()
            process.join(0.2)
            if process.is_alive():
                process.kill()
                process.join(0.2)
            latest.update(
                result="TIMEOUT",
                timeout_seconds=timeout,
                timed_out_phase="query" if ready_time is not None else "setup",
            )
        elif process.is_alive():
            process.join(0.2)
        else:
            process.join(0.2)
        while receiver.poll():
            try:
                kind, payload = receiver.recv()
            except (EOFError, OSError):
                break
            if kind in {"READY", "SOLVE_STARTED", "RESULT"}:
                latest = payload
                if kind == "READY":
                    ready_time = ready_time or time.monotonic()
                if kind == "RESULT":
                    received_result = True
        if not received_result and not timed_out:
            latest.update(result="ERROR", error=f"Probe worker exited with code {process.exitcode}")
        latest["worker_wall_time"] = time.monotonic() - started
        if ready_time is not None and latest.get("result") != "TIMEOUT":
            latest["query_wall_time"] = latest.get("propagation_time", 0.0) + latest.get("solve_time", 0.0)
        return latest
    finally:
        if process.is_alive():
            process.kill()
            process.join(0.2)
        receiver.close()
        process.close()


def _unsat(result):
    return result in UNSAT_RESULTS


def traverse_prefix_trie(root, probe, depth_budgets, promote_time=0.0):
    """Visit by coverage, descending after timeout and pruning after UNSAT."""
    probes = []
    sat_result = None
    fatal_result = None

    def visit_children(parent, parent_status):
        nonlocal sat_result, fatal_result
        for vertex, node in ordered_children(parent):
            result = probe(node.prefix, depth_budgets[node.depth - 1], node)
            node.result = result["result"]
            node.probe_index = len(probes)
            record = {
                **result,
                "index": node.probe_index,
                "depth": node.depth,
                "parent_status": parent_status,
                "source_count": node.source_count,
                "best_source_k": node.best_source_k,
                "vertex": vertex,
            }
            probes.append(record)
            if node.result == "SAT":
                sat_result = record
                return
            if node.result == "ERROR":
                fatal_result = record
                return
            if not _unsat(node.result) and node.children:
                visit_children(node, node.result)
                if sat_result is not None or fatal_result is not None:
                    return

    visit_children(root, "ROOT")

    if promote_time > 0 and sat_result is None and fatal_result is None:
        promotions = []

        def collect_promotions(node):
            for child in node.children.values():
                unsat_children = sum(_unsat(grandchild.result) for grandchild in child.children.values())
                if child.result == "TIMEOUT" and unsat_children >= 2:
                    promotions.append(child)
                collect_promotions(child)

        collect_promotions(root)
        promotions.sort(key=lambda node: (-node.source_count, node.depth, node.prefix))
        for node in promotions:
            result = probe(node.prefix, promote_time, node)
            node.promotion_result = result["result"]
            record = {
                **result,
                "index": len(probes),
                "depth": node.depth,
                "parent_status": "PROMOTION",
                "source_count": node.source_count,
                "best_source_k": node.best_source_k,
                "promotion": True,
            }
            probes.append(record)
            if result["result"] == "SAT":
                sat_result = record
                break
            if result["result"] == "ERROR":
                fatal_result = record
                break
            if _unsat(result["result"]):
                node.result = result["result"]

    learned = []
    pruned_descendants = 0
    pruned_schedules = 0

    def collect_learned(parent):
        nonlocal pruned_descendants, pruned_schedules
        for _, node in ordered_children(parent):
            if _unsat(node.result):
                descendants = _node_count(node) - 1
                pruned_descendants += descendants
                pruned_schedules += node.source_count
                learned.append(
                    {
                        "prefix": [list(item) for item in node.prefix],
                        "depth": node.depth,
                        "source_count": node.source_count,
                        "best_source_k": node.best_source_k,
                        "result": node.result,
                        "promoted": node.promotion_result == node.result,
                        "pruned_descendant_nodes": descendants,
                        "pruned_schedules": node.source_count,
                    }
                )
            else:
                collect_learned(node)

    collect_learned(root)
    return {
        "probes": probes,
        "learned_prefixes": learned,
        "sat_result": sat_result,
        "fatal_result": fatal_result,
        "pruned_descendant_nodes": pruned_descendants,
        "pruned_schedules": pruned_schedules,
    }


def _depth_summary(probes, maximum_depth):
    summary = {}
    for depth in range(1, maximum_depth + 1):
        rows = [row for row in probes if row.get("depth") == depth]
        summary[str(depth)] = {
            "probes": len(rows),
            "sat": sum(row.get("result") == "SAT" for row in rows),
            "unsat_propagation": sum(row.get("result") == "UNSAT_PROPAGATION" for row in rows),
            "unsat_solve": sum(row.get("result") == "UNSAT_SOLVE" for row in rows),
            "timeout": sum(row.get("result") == "TIMEOUT" for row in rows),
            "errors": sum(row.get("result") == "ERROR" for row in rows),
            "promotions": sum(row.get("promotion") is True for row in rows),
        }
    return summary


def _node_counts(root, maximum_depth):
    counts = {depth: 0 for depth in range(1, maximum_depth + 1)}

    def visit(node):
        for child in node.children.values():
            counts[child.depth] += 1
            visit(child)

    visit(root)
    return {str(depth): count for depth, count in counts.items()}


def run_prefix_trie_mining(
    path,
    firefighters,
    solver_name,
    horizon,
    bound,
    schedule_path,
    pool_size=128,
    prefix_depth=5,
    depth_budgets=(60.0, 20.0, 10.0, 2.0, 1.0),
    seed=0,
    replay_bounds=(991, 990),
    replay_timeout=90.0,
    final_timeout=600.0,
    jobs=1,
    promote_time=0.0,
):
    if firefighters != 1:
        raise ValueError("The current semantic trie is defined for D=1 action prefixes")
    if jobs != 1:
        raise ValueError("The initial trie-mining experiment is serial; use --jobs 1")
    if len(depth_budgets) != prefix_depth:
        raise ValueError("--depth-budgets must provide exactly one budget per prefix depth")
    report = {
        "query": {"T": horizon, "K": bound},
        "config": {
            "instance": str(path),
            "firefighters": firefighters,
            "solver": solver_name,
            "schedule": str(schedule_path),
            "pool_size": pool_size,
            "prefix_depth": prefix_depth,
            "depth_budgets": list(depth_budgets),
            "seed": seed,
            "jobs": jobs,
            "replay_bounds": list(replay_bounds),
            "replay_query_time": replay_timeout,
            "final_query_time": final_timeout,
            "promote_time": promote_time,
        },
        "sanity_probes": [],
        "trie": {},
        "master_comparison": {},
        "result": "RUNNING",
    }
    schedule_data = json.loads(Path(schedule_path).read_text(encoding="utf-8"))
    records = _schedule_records(schedule_data)
    if not records:
        raise ValueError("Schedule JSON must contain a schedule")
    instance = read_instance(path)
    schedule = records[0][0]
    solution = simulate(instance, firefighters, schedule)
    expected_k = records[0][1]
    if expected_k is not None and expected_k != solution.k:
        raise ValueError(f"Input schedule validates to K={solution.k}, expected K={expected_k}")

    for depth in (3, 5):
        if depth > prefix_depth or depth > len(solution.schedule):
            raise ValueError(f"Known-incumbent sanity depth {depth} is unavailable")
        prefix = tuple(
            (round_number, row[0])
            for round_number, row in enumerate(solution.schedule[:depth], start=1)
        )
        probe = run_trie_probe(
            path,
            firefighters,
            solver_name,
            horizon,
            bound,
            prefix,
            depth_budgets[depth - 1],
        )
        probe.update(depth=depth, source="known_incumbent", source_k=solution.k)
        report["sanity_probes"].append(probe)
        print(f"prefix-trie sanity depth={depth}: {probe['result']}", flush=True)
        if probe["result"] not in UNSAT_RESULTS:
            report.update(result="SANITY_FAILED", reason=f"Known prefix depth {depth} was not proven UNSAT")
            return report

    instance, pool, pool_stats = build_prefix_pool(
        path, firefighters, schedule_path, pool_size, prefix_depth, seed
    )
    root = build_prefix_trie(pool, prefix_depth)
    def probe_and_log(prefix, budget, node):
        result = run_trie_probe(
            path, firefighters, solver_name, horizon, bound, prefix, budget
        )
        print(
            f"prefix-trie depth={node.depth} coverage={node.source_count} "
            f"prefix={prefix} result={result['result']} "
            f"query={result.get('query_wall_time', result.get('worker_wall_time', 0.0)):.3f}s",
            flush=True,
        )
        return result

    traversal = traverse_prefix_trie(
        root,
        probe_and_log,
        depth_budgets,
        promote_time=promote_time,
    )
    probes = traversal["probes"]
    learned = traversal["learned_prefixes"]
    depth_counts = _depth_summary(probes, prefix_depth)
    histogram = {}
    for prefix in learned:
        key = str(prefix["depth"])
        histogram[key] = histogram.get(key, 0) + 1
    propagation_unsat = sum(row["result"] == "UNSAT_PROPAGATION" for row in probes)
    solve_unsat = sum(row["result"] == "UNSAT_SOLVE" for row in probes)
    report["trie"] = {
        **pool_stats,
        "nodes_by_depth": _node_counts(root, prefix_depth),
        "probes_by_depth": depth_counts,
        "probes": probes,
        "propagation_unsat_count": propagation_unsat,
        "solved_unsat_count": solve_unsat,
        "shortest_proved_unsat_by_depth": histogram,
        "shortest_proved_unsat_prefixes": learned,
        "pruned_descendant_nodes": traversal["pruned_descendant_nodes"],
        "pruned_schedules": traversal["pruned_schedules"],
        "unsat_prefix_length_histogram": histogram,
    }
    if traversal["sat_result"] is not None:
        report.update(
            result="SAT_INCUMBENT_FOUND",
            incumbent=traversal["sat_result"],
            reason="A trie prefix probe found a simulator-validated target-bound schedule",
        )
        return report
    if traversal["fatal_result"] is not None:
        report.update(result="PROBE_ERROR", error=traversal["fatal_result"].get("error"))
        return report

    semantic_prefixes = [
        [(round_number, vertex, True) for round_number, vertex in row["prefix"]]
        for row in learned
    ]
    base = run_master_replay(
        path, firefighters, solver_name, horizon, bound, replay_bounds,
        "base", (), replay_timeout, final_timeout,
    )
    report["master_comparison"]["base"] = base
    if base.get("pre_append_signature") is None:
        report.update(result="MASTER_BASE_FAILED", reason=base.get("result"))
        return report
    mined = run_master_replay(
        path, firefighters, solver_name, horizon, bound, replay_bounds,
        "trie-mined", semantic_prefixes, replay_timeout, final_timeout,
        expected_history=base["pre_append_signature"],
    )
    mined["history_mismatch"] = mined.get("result") == "HISTORY_MISMATCH"
    mined["history_matches_base"] = (
        not mined["history_mismatch"]
        and mined.get("pre_append_signature") == base["pre_append_signature"]
    )
    report["master_comparison"]["trie-mined"] = mined
    if mined.get("result") == "HISTORY_MISMATCH":
        report.update(result="HISTORY_MISMATCH", reason="Replay differed before trie clause append")
    else:
        report["result"] = "COMPLETED"
    return report
