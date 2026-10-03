"""Diagnostics for learning guarded clauses from UNSAT action prefixes."""

from __future__ import annotations

import json
import multiprocessing
import random
import statistics
import time
from collections import Counter
from pathlib import Path

from pysat.solvers import Solver

from .encoder import Encoder
from .heuristic import threat
from .instance import read_instance
from .preprocess import preprocess
from .simulator import simulate


def semantic_prefix(schedule, depth):
    """Return a schedule prefix as ordered (round, vertex) pairs."""
    return tuple(
        (round_number, vertex)
        for round_number, actions in enumerate(schedule[:depth], start=1)
        for vertex in sorted(actions)
    )


def semantic_core(core, encoder, supplied_literals):
    """Map solver literals back to (round, vertex, polarity), checking provenance."""
    allowed = set(supplied_literals)
    if not set(core).issubset(allowed):
        raise ValueError("Solver returned a core literal that was not a prefix assumption")
    reverse = {variable: (vertex, round_number) for (vertex, round_number), variable in encoder.a.items()}
    result = []
    for literal in core:
        variable = abs(literal)
        if variable not in reverse:
            raise ValueError(f"Core literal {literal} is not an action variable")
        vertex, round_number = reverse[variable]
        result.append((round_number, vertex, literal > 0))
    return tuple(sorted(set(result)))


def guarded_core_clause(query_assumptions, core, encoder):
    """Map a semantic core into a query-guarded master clause."""
    clause = [-literal for literal in query_assumptions]
    for round_number, vertex, positive in core:
        action = encoder.a[vertex, round_number]
        literal = action if positive else -action
        clause.append(-literal)
    # Stable deduplication also makes malformed duplicate assumptions harmless.
    return list(dict.fromkeys(clause))


def non_subsumed_cores(raw_cores):
    """Deduplicate semantic cores and retain only subset-minimal cores."""
    retained = []
    for raw in raw_cores:
        core = frozenset(tuple(item) for item in raw)
        if any(existing <= core for existing in retained):
            continue
        retained = [existing for existing in retained if not core < existing]
        retained.append(core)
    return sorted((tuple(sorted(core)) for core in retained), key=lambda core: (len(core), core))


def _schedule_records(schedule_data):
    records = []
    if isinstance(schedule_data, dict):
        if isinstance(schedule_data.get("schedule"), list):
            records.append((schedule_data["schedule"], schedule_data.get("best_k")))
        for entry in schedule_data.get("pareto_frontier", []):
            if isinstance(entry, dict) and isinstance(entry.get("schedule"), list):
                records.append((entry["schedule"], entry.get("k")))
    elif isinstance(schedule_data, list):
        records.append((schedule_data, None))
    return records


def _prefix_candidate(instance, firefighters, schedule, depth, source):
    solution = simulate(instance, firefighters, schedule)
    prefix = semantic_prefix(solution.schedule, depth)
    if not prefix:
        return None
    return {
        "prefix": [[round_number, vertex] for round_number, vertex in prefix],
        "source_k": solution.k,
        "source_containment_time": solution.containment_time,
        "actual_depth": len({round_number for round_number, _ in prefix}),
        "source": source,
        "schedule": [list(actions) for actions in solution.schedule],
        "_key": prefix,
    }


def build_prefix_pool(path, firefighters, schedule_path, pool_size, depth, seed):
    """Build a deterministic, K-prioritized pool with prefix diversity."""
    instance = read_instance(path)
    data = json.loads(Path(schedule_path).read_text(encoding="utf-8"))
    candidate_by_key = {}
    supplied = _schedule_records(data)
    for index, (schedule, expected_k) in enumerate(supplied):
        candidate = _prefix_candidate(instance, firefighters, schedule, depth, f"input:{index}")
        if candidate is None:
            continue
        if expected_k is not None and candidate["source_k"] != expected_k:
            raise ValueError(
                f"Input schedule validates to K={candidate['source_k']}, expected K={expected_k}"
            )
        candidate_by_key.setdefault(tuple(map(tuple, candidate["prefix"])), candidate)

    rng = random.Random(seed)
    attempts = 0
    max_attempts = max(1000, pool_size * 30)
    while len(candidate_by_key) < max(pool_size * 4, pool_size) and attempts < max_attempts:
        attempts += 1
        solution = threat(instance, firefighters, mode="random", rng=rng)
        candidate = _prefix_candidate(
            instance, firefighters, solution.schedule, depth, f"random:{seed}:{attempts}"
        )
        if candidate is not None:
            candidate_by_key.setdefault(tuple(map(tuple, candidate["prefix"])), candidate)

    candidates = list(candidate_by_key.values())
    candidates.sort(key=lambda row: (row["source_k"], row["source_containment_time"], row["_key"]))
    if len(candidates) <= pool_size:
        selected = candidates
    else:
        # Start with the best K, then greedily prefer prefixes far from those
        # already selected. K and semantic order break diversity ties.
        selected = [candidates.pop(0)]
        while candidates and len(selected) < pool_size:
            selected_key_sets = [set(row["_key"]) for row in selected]

            def diversity_score(row):
                key = set(row["_key"])
                distance = min(len(key.symmetric_difference(other)) for other in selected_key_sets)
                return distance, -row["source_k"], -row["source_containment_time"], row["_key"]

            best = max(candidates, key=diversity_score)
            selected.append(best)
            candidates.remove(best)
    for row in selected:
        row.pop("_key", None)
    return instance, selected, {
        "generated_schedules": attempts + len(supplied),
        "unique_prefixes": len(candidate_by_key),
        "selected_prefixes": len(selected),
        "pool_size_requested": pool_size,
        "prefix_depth": depth,
        "seed": seed,
    }


def _stats(solver, before):
    after = solver.accum_stats()
    return {
        key: after.get(key, 0) - before.get(key, 0)
        for key in ("decisions", "conflicts", "propagations", "restarts")
    }


def _prefix_probe_worker(connection, path, firefighters, solver_name, horizon, bound, prefix):
    solver = encoder = None
    result = {
        "experiment": "prefix_core_probe",
        "T": horizon,
        "K": bound,
        "prefix": prefix,
        "result": "ERROR",
    }
    try:
        instance = read_instance(path)
        distance, _ = preprocess(instance, firefighters)
        solver = Solver(name=solver_name)
        if not hasattr(solver, "get_core"):
            raise RuntimeError(f"Solver backend {solver_name!r} does not support get_core()")
        encoder = Encoder(instance, firefighters, solver, distance)
        encode_started = time.monotonic()
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
            encoding_time=time.monotonic() - encode_started,
        )
        before = solver.accum_stats()
        solve_started = time.monotonic()
        satisfiable = solver.solve(assumptions=prefix_literals)
        result["solve_time"] = time.monotonic() - solve_started
        result["stats"] = _stats(solver, before)
        if satisfiable:
            model = set(solver.get_model())
            schedule = [list(actions) for actions in encoder.decode(model, horizon)]
            solution = simulate(instance, firefighters, schedule)
            if solution.k > bound or solution.containment_time > horizon:
                raise AssertionError("SAT prefix probe failed simulator validation")
            result.update(
                result="SAT",
                actual_k=solution.k,
                containment_time=solution.containment_time,
                schedule=[list(actions) for actions in solution.schedule],
            )
        else:
            core = solver.get_core()
            if core is None:
                result["core_api_returned_none"] = True
                semantic = ()
            else:
                semantic = semantic_core(core, encoder, prefix_literals)
            result.update(
                raw_core=None if core is None else list(core),
                core=[list(item) for item in semantic],
            )
            if not core:
                connection.send(("EMPTY_CORE", result))
                confirm_before = solver.accum_stats()
                confirm_started = time.monotonic()
                confirmed_unsat = solver.solve()
                result["empty_core_confirmation_time"] = time.monotonic() - confirm_started
                result["empty_core_confirmation_stats"] = _stats(solver, confirm_before)
                if confirmed_unsat:
                    if core is None:
                        raise RuntimeError(
                            "Solver returned no core although the query without prefix assumptions is SAT"
                        )
                    raise AssertionError("Empty core was not confirmed by the no-assumption query")
                result.update(result="QUERY_UNSAT", query_unsat_confirmed=True)
            else:
                result["result"] = "UNSAT"
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


def run_prefix_probe(path, firefighters, solver_name, horizon, bound, prefix, timeout):
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(
        target=_prefix_probe_worker,
        args=(sender, str(path), firefighters, solver_name, horizon, bound, prefix),
    )
    started = time.monotonic()
    process.start()
    sender.close()
    latest = {"prefix": prefix, "result": "ERROR"}
    empty_core_seen = False
    received = False
    try:
        while process.is_alive() and time.monotonic() - started < timeout:
            if not receiver.poll(0.05):
                continue
            try:
                kind, payload = receiver.recv()
            except (EOFError, OSError):
                break
            if kind == "EMPTY_CORE":
                latest = payload
                empty_core_seen = True
            elif kind == "RESULT":
                latest = payload
                received = True
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
            if kind == "EMPTY_CORE":
                latest = payload
                empty_core_seen = True
            elif kind == "RESULT":
                latest = payload
                received = True
        elapsed = time.monotonic() - started
        if not received:
            if timed_out:
                latest.update(
                    result="EMPTY_CORE_UNCONFIRMED" if empty_core_seen else "TIMEOUT",
                    wall_time=elapsed,
                    timed_out=True,
                )
            else:
                latest.update(
                    result="ERROR",
                    error=f"Probe worker exited with code {process.exitcode} without a result",
                    wall_time=elapsed,
                    timed_out=False,
                )
        else:
            latest["wall_time"] = elapsed
        return latest
    finally:
        if process.is_alive():
            process.kill()
            process.join(0.2)
        receiver.close()
        process.close()


def _guard_signature(assumptions, core):
    """Return the stable semantic ingredients for a guarded blocking clause."""
    return {"query_assumptions": list(assumptions), "core": [list(item) for item in core]}


def _master_replay_worker(
    connection,
    path,
    firefighters,
    solver_name,
    horizon,
    final_bound,
    replay_bounds,
    mode,
    cores,
    expected_history,
):
    solver = encoder = None
    metrics = {"mode": mode, "result": "ERROR", "stages": [], "imported_core_clauses": []}
    try:
        instance = read_instance(path)
        distance, _ = preprocess(instance, firefighters)
        solver = Solver(name=solver_name)
        encoder = Encoder(instance, firefighters, solver, distance)
        encoder.ensure_horizon(horizon)
        for index, bound in enumerate(replay_bounds):
            assumptions = encoder.assumptions(horizon, bound, instance.n)
            stage = {
                "index": index,
                "stage": "replay",
                "K": bound,
                "assumptions": list(assumptions),
                "variables_before_solve": encoder.vars.top,
                "clauses_before_solve": encoder.clauses,
            }
            metrics["stages"].append(stage)
            connection.send(("PROGRESS", metrics))
            connection.send(("STAGE_STARTED", {"index": index, "stage": "replay", "K": bound}))
            before = solver.accum_stats()
            started = time.monotonic()
            satisfiable = solver.solve(assumptions=assumptions)
            stage.update(
                result="SAT" if satisfiable else "UNSAT",
                solve_time=time.monotonic() - started,
                stats=_stats(solver, before),
            )
            connection.send(("STAGE_SOLVED", {"index": index}))
            if satisfiable:
                model = set(solver.get_model())
                schedule = [list(actions) for actions in encoder.decode(model, horizon)]
                solution = simulate(instance, firefighters, schedule)
                if solution.k > bound or solution.containment_time > horizon:
                    raise AssertionError(f"Master replay SAT model at K={bound} failed simulation")
                stage.update(
                    actual_k=solution.k,
                    containment_time=solution.containment_time,
                    schedule=[list(actions) for actions in solution.schedule],
                )
            connection.send(("STAGE_RESULT", stage))
            if not satisfiable:
                metrics.update(result="REPLAY_UNSAT", failed_bound=bound)
                connection.send(("FINAL", metrics))
                return

        assumptions = encoder.assumptions(horizon, final_bound, instance.n)
        signature = {
            "history": [
                {
                    key: stage.get(key)
                    for key in (
                        "K", "result", "stats", "variables_before_solve",
                        "clauses_before_solve", "assumptions",
                    )
                }
                for stage in metrics["stages"]
            ],
            "variables": encoder.vars.top,
            "clauses": encoder.clauses,
            "assumptions": list(assumptions),
        }
        metrics["pre_append_signature"] = signature
        connection.send(("PROGRESS", metrics))
        if expected_history is not None and signature != expected_history:
            metrics.update(result="HISTORY_MISMATCH", history_mismatch=True)
            connection.send(("FINAL", metrics))
            return

        if mode == "mined":
            for core in cores:
                clause = guarded_core_clause(assumptions, core, encoder)
                solver.add_clause(clause)
                metrics["imported_core_clauses"].append(
                    {"semantic_core": [list(item) for item in core], "clause": clause}
                )
        metrics.update(
            imported_clause_count=len(metrics["imported_core_clauses"]),
            imported_clause_literals=sum(
                len(entry["clause"]) for entry in metrics["imported_core_clauses"]
            ),
            final_variables=encoder.vars.top,
            final_clauses=encoder.clauses + len(metrics["imported_core_clauses"]),
        )
        final_index = len(replay_bounds)
        stage = {
            "index": final_index,
            "stage": "final",
            "K": final_bound,
            "assumptions": list(assumptions),
            "variables_before_solve": encoder.vars.top,
            "clauses_before_solve": encoder.clauses + len(metrics["imported_core_clauses"]),
            "imported_core_clauses": len(metrics["imported_core_clauses"]),
        }
        metrics["stages"].append(stage)
        connection.send(("PROGRESS", metrics))
        connection.send(("STAGE_STARTED", {"index": final_index, "stage": "final", "K": final_bound}))
        before = solver.accum_stats()
        started = time.monotonic()
        satisfiable = solver.solve(assumptions=assumptions)
        stage.update(
            result="SAT" if satisfiable else "UNSAT",
            solve_time=time.monotonic() - started,
            stats=_stats(solver, before),
        )
        connection.send(("STAGE_SOLVED", {"index": final_index}))
        if satisfiable:
            model = set(solver.get_model())
            schedule = [list(actions) for actions in encoder.decode(model, horizon)]
            solution = simulate(instance, firefighters, schedule)
            if solution.k > final_bound or solution.containment_time > horizon:
                raise AssertionError("Master final SAT model failed simulator validation")
            stage.update(
                actual_k=solution.k,
                containment_time=solution.containment_time,
                schedule=[list(actions) for actions in solution.schedule],
            )
        connection.send(("STAGE_RESULT", stage))
        metrics.update(result=stage["result"])
        connection.send(("FINAL", metrics))
    except Exception as exc:
        metrics.update(result="ERROR", error=f"{type(exc).__name__}: {exc}")
        connection.send(("ERROR", metrics))
    finally:
        if encoder is not None:
            encoder.close()
        if solver is not None:
            solver.delete()
        connection.close()


def run_master_replay(
    path,
    firefighters,
    solver_name,
    horizon,
    final_bound,
    replay_bounds,
    mode,
    cores,
    replay_timeout,
    final_timeout,
    expected_history=None,
):
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(
        target=_master_replay_worker,
        args=(
            sender, str(path), firefighters, solver_name, horizon, final_bound,
            tuple(replay_bounds), mode, cores, expected_history,
        ),
    )
    started = time.monotonic()
    process.start()
    sender.close()
    latest = {"mode": mode, "result": "ERROR", "stages": []}
    active_stage = None
    stage_started = None
    complete = False

    def merge(payload):
        old = {row.get("index"): row for row in latest.get("stages", [])}
        for row in payload.get("stages", []):
            old[row.get("index")] = row
        latest.update(payload)
        latest["stages"] = [old[key] for key in sorted(old) if key is not None]

    try:
        while process.is_alive():
            if active_stage is None:
                if not receiver.poll(0.05):
                    continue
                try:
                    kind, payload = receiver.recv()
                except (EOFError, OSError):
                    break
            else:
                budget = final_timeout if active_stage["stage"] == "final" else replay_timeout
                remaining = budget - (time.monotonic() - stage_started)
                if remaining <= 0:
                    timed = dict(active_stage)
                    process.terminate()
                    process.join(0.2)
                    if process.is_alive():
                        process.kill()
                        process.join(0.2)
                    latest.update(
                        result="TIMEOUT" if timed["stage"] == "final" else "REPLAY_TIMEOUT",
                        failed_stage=timed["stage"],
                        failed_bound=timed["K"],
                    )
                    row = next((r for r in latest.get("stages", []) if r.get("index") == timed["index"]), {})
                    row.update(timed, result="TIMEOUT", solve_time=budget, stats=None)
                    latest["stages"] = [r for r in latest.get("stages", []) if r.get("index") != timed["index"]]
                    latest["stages"].append(row)
                    latest["stages"].sort(key=lambda r: r.get("index", 0))
                    break
                if not receiver.poll(min(0.05, remaining)):
                    continue
                try:
                    kind, payload = receiver.recv()
                except (EOFError, OSError):
                    break
            if kind == "PROGRESS":
                merge(payload)
            elif kind == "STAGE_STARTED":
                active_stage = payload
                stage_started = time.monotonic()
            elif kind == "STAGE_SOLVED":
                active_stage = None
                stage_started = None
            elif kind == "STAGE_RESULT":
                rows = [r for r in latest.get("stages", []) if r.get("index") != payload.get("index")]
                rows.append(payload)
                latest["stages"] = sorted(rows, key=lambda r: r.get("index", 0))
                active_stage = None
                stage_started = None
            elif kind == "FINAL":
                merge(payload)
                complete = True
                active_stage = None
            elif kind == "ERROR":
                merge(payload)
                complete = True
                active_stage = None
        if process.is_alive():
            process.join(0.2)
        while receiver.poll():
            try:
                kind, payload = receiver.recv()
            except (EOFError, OSError):
                break
            if kind in {"PROGRESS", "FINAL", "ERROR"}:
                merge(payload)
                complete = kind != "PROGRESS"
            elif kind == "STAGE_RESULT":
                rows = [r for r in latest.get("stages", []) if r.get("index") != payload.get("index")]
                rows.append(payload)
                latest["stages"] = sorted(rows, key=lambda r: r.get("index", 0))
        if not complete and "failed_stage" not in latest:
            latest.update(result="ERROR", error=f"Worker exited with code {process.exitcode}")
        latest["wall_time"] = time.monotonic() - started
        return latest
    finally:
        if process.is_alive():
            process.kill()
            process.join(0.2)
        receiver.close()
        process.close()


def master_history_signature(case):
    signature = case.get("pre_append_signature")
    if signature is None:
        raise ValueError(f"Missing pre-append signature for {case.get('mode')}")
    return signature


def summarize_cores(raw_cores):
    unique = {tuple(sorted(tuple(item) for item in core)) for core in raw_cores}
    filtered = non_subsumed_cores(raw_cores)
    histogram = Counter(len(core) for core in raw_cores)
    actions = Counter(tuple(item) for core in raw_cores for item in core)
    return {
        "raw_cores": len(raw_cores),
        "unique_cores": len(unique),
        "non_subsumed_cores": len(filtered),
        "core_size_histogram": {str(key): value for key, value in sorted(histogram.items())},
        "action_frequency": [
            {"round": row[0], "vertex": row[1], "positive": row[2], "count": count}
            for row, count in actions.most_common()
        ],
        "filtered_cores": [[list(item) for item in core] for core in filtered],
    }


def run_prefix_core_mining(
    path,
    firefighters,
    solver_name,
    horizon,
    bound,
    schedule_path,
    sanity_depths=(3, 5),
    pool_size=128,
    prefix_depth=5,
    probe_timeout=5.0,
    sanity_timeout=10.0,
    seed=0,
    replay_bounds=(991, 990),
    replay_timeout=90.0,
    final_timeout=600.0,
    jobs=1,
    sanity_only=False,
):
    if jobs != 1:
        raise ValueError("The initial prefix-core diagnostic is serial; use --jobs 1")
    report = {
        "query": {"T": horizon, "K": bound},
        "config": {
            "instance": str(path),
            "firefighters": firefighters,
            "solver": solver_name,
            "schedule": str(schedule_path),
            "pool_size": pool_size,
            "prefix_depth": prefix_depth,
            "probe_time": probe_timeout,
            "sanity_time": sanity_timeout,
            "seed": seed,
            "jobs": jobs,
            "replay_bounds": list(replay_bounds),
            "replay_query_time": replay_timeout,
            "final_query_time": final_timeout,
        },
        "sanity_probes": [],
        "mining": {},
        "master_comparison": {},
        "result": "RUNNING",
    }
    data = json.loads(Path(schedule_path).read_text(encoding="utf-8"))
    records = _schedule_records(data)
    if not records:
        raise ValueError("Schedule JSON must contain a schedule")
    instance = read_instance(path)
    reference_schedule = records[0][0]
    verified = simulate(instance, firefighters, reference_schedule)
    expected_k = records[0][1]
    if expected_k is not None and verified.k != expected_k:
        raise ValueError(f"Input schedule validates to K={verified.k}, expected K={expected_k}")

    # Run the known fixed prefixes first. They also verify core extraction on
    # exactly this query and supplied prefix assumptions only.
    for depth in sanity_depths:
        candidate = _prefix_candidate(instance, firefighters, reference_schedule, depth, "sanity")
        if candidate is None:
            raise ValueError(f"Sanity prefix depth {depth} produced no actions")
        probe = run_prefix_probe(
            path,
            firefighters,
            solver_name,
            horizon,
            bound,
            candidate["prefix"],
            sanity_timeout,
        )
        probe.update(depth=depth, source_k=verified.k, source="known_incumbent")
        report["sanity_probes"].append(probe)
        print(f"prefix-core sanity depth={depth}: {probe['result']}", flush=True)
        if probe["result"] != "UNSAT" or not set(probe.get("raw_core", ())).issubset(
            set(probe.get("prefix_literals", ()))
        ):
            report.update(result="SANITY_FAILED", reason=f"Expected an UNSAT core at depth {depth}")
            return report
    if sanity_only:
        report["result"] = "SANITY_PASSED"
        return report

    instance, pool, pool_stats = build_prefix_pool(
        path, firefighters, schedule_path, pool_size, prefix_depth, seed
    )
    raw_cores = []
    probes = []
    counts = Counter()
    unsat_times = []
    shortcut_result = None
    for index, candidate in enumerate(pool):
        probe = run_prefix_probe(
            path,
            firefighters,
            solver_name,
            horizon,
            bound,
            candidate["prefix"],
            probe_timeout,
        )
        probe.update(
            index=index,
            source_k=candidate["source_k"],
            source_containment_time=candidate["source_containment_time"],
            actual_depth=candidate["actual_depth"],
            source=candidate["source"],
        )
        probes.append(probe)
        counts[probe["result"]] += 1
        if probe["result"] == "UNSAT":
            raw_cores.append(probe["core"])
            unsat_times.append(probe.get("solve_time", 0.0))
        print(
            f"prefix-core probe {index + 1}/{len(pool)}: {probe['result']} "
            f"depth={probe['actual_depth']} time={probe.get('solve_time', 0.0):.3f}s",
            flush=True,
        )
        if probe["result"] == "SAT":
            shortcut_result = "SAT_INCUMBENT_FOUND"
            break
        if probe["result"] in {"QUERY_UNSAT", "EMPTY_CORE_UNCONFIRMED", "ERROR"}:
            shortcut_result = probe["result"]
            break

    core_summary = summarize_cores(raw_cores)
    mining = {
        **pool_stats,
        "probed_prefixes": len(probes),
        "sat": counts["SAT"],
        "unsat": counts["UNSAT"],
        "timeout": counts["TIMEOUT"],
        "errors": counts["ERROR"],
        "query_unsat_confirmed": counts["QUERY_UNSAT"],
        "empty_core_unconfirmed": counts["EMPTY_CORE_UNCONFIRMED"],
        "mean_unsat_solve_time": statistics.fmean(unsat_times) if unsat_times else None,
        "median_unsat_solve_time": statistics.median(unsat_times) if unsat_times else None,
        **core_summary,
    }
    report["probes"] = probes
    report["mining"] = mining
    if shortcut_result is not None:
        report["result"] = shortcut_result
        report["reason"] = "Mining found a SAT incumbent or a confirmed/unconfirmed empty core"
        return report

    cores = mining["filtered_cores"]
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
        "mined", cores, replay_timeout, final_timeout,
        expected_history=master_history_signature(base),
    )
    mined["history_mismatch"] = mined.get("result") == "HISTORY_MISMATCH"
    mined["history_matches_base"] = (
        not mined["history_mismatch"]
        and mined.get("pre_append_signature") == master_history_signature(base)
    )
    report["master_comparison"]["mined"] = mined
    if mined.get("result") == "HISTORY_MISMATCH":
        report.update(result="HISTORY_MISMATCH", reason="Base replay or final query differed before core append")
    else:
        report["result"] = "COMPLETED"
    return report
