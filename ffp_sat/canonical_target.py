"""Objective-free exact target encoding diagnostic for one-firefighter FFP."""

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
from .objective import IncrementalAtLeastCounter
from .preprocess import preprocess
from .simulator import simulate


def _small_instances(seed=0):
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
            initial = {v for v in range(n) if rng.random() < 0.2}
            if not initial:
                initial.add(rng.randrange(n))
            if n - len(initial) >= 2:
                instances.append(Instance(tuple(map(frozenset, adjacency)), frozenset(initial)))
    return instances


def _append_canonical_target(encoder, solver, horizon, untouched_required=2):
    """Append exact-action clauses and one-way untouched witnesses.

    For D=1 and n-K=T+2, any feasible objective model with r actions and q
    untouched vertices has q-2 >= T-r. Fill every idle action slot with a
    distinct non-witness untouched vertex. This only blocks fire earlier, so
    it preserves containment and the two selected untouched vertices.
    """
    if encoder.firefighters != 1:
        raise ValueError("Canonical target encoding currently requires one firefighter")
    if horizon <= 0 or horizon > encoder.horizon:
        raise ValueError("Canonical target horizon must already be encoded")
    added_rounds = set(getattr(encoder, "_canonical_target_rounds", set()))
    clauses_before = encoder.clauses
    variables_before = encoder.vars.top
    added_action_clauses = []
    for round_number in range(1, horizon + 1):
        if round_number not in added_rounds:
            encoder.add([encoder.a[v, round_number] for v in range(encoder.instance.n)])
            added_rounds.add(round_number)
            added_action_clauses.append(round_number)
    encoder._canonical_target_rounds = added_rounds

    witness_before = encoder.vars.top
    witnesses = [encoder.vars.new_aux(f"untouched_witness[{v},{horizon}]") for v in range(encoder.instance.n)]
    for vertex, witness in enumerate(witnesses):
        encoder.add([-witness, -encoder.b[vertex, horizon]])
        encoder.add([-witness, -encoder.d[vertex, horizon]])
    witness_variables = encoder.vars.top - witness_before

    counter = IncrementalAtLeastCounter(
        witnesses,
        encoder.vars,
        lambda clause: encoder.add(clause),
        horizon=f"untouched_at_least_{horizon}",
    )
    witness_assumption = counter.assumption(untouched_required)
    return {
        "action_rounds": list(range(1, horizon + 1)),
        "action_clauses_added": added_action_clauses,
        "exact_action_clauses": len(added_action_clauses),
        "untouched_witness_variables": witness_variables,
        "untouched_counter_auxiliary_variables": counter.auxiliary_variables,
        "untouched_counter_clauses": counter.number_of_clauses,
        "variables_added": encoder.vars.top - variables_before,
        "clauses_added": encoder.clauses - clauses_before,
        "untouched_assumption": witness_assumption,
        "witness_literals": witnesses,
        "counter": counter,
    }


def _canonical_target_query(instance, horizon, solver_name, connection=None):
    distance, _ = preprocess(instance, 1)
    with Solver(name=solver_name) as solver:
        encoder = Encoder(instance, 1, solver, distance)
        try:
            started = time.monotonic()
            encoder.ensure_horizon(horizon)
            containment = encoder.ensure_containment(horizon)
            before_canonical_vars = encoder.vars.top
            before_canonical_clauses = encoder.clauses
            canonical = _append_canonical_target(encoder, solver, horizon)
            assumptions = [containment, canonical["untouched_assumption"]]
            if encoder.objectives:
                raise AssertionError("Fresh canonical-target formula unexpectedly has an objective")
            if connection is not None:
                connection.send(
                    (
                        "PROGRESS",
                        {
                            "mode": "fresh",
                            "base_variables": before_canonical_vars,
                            "base_clauses": before_canonical_clauses,
                            "variables": encoder.vars.top,
                            "clauses": encoder.clauses,
                            "objective_aux_variables": 0,
                            "objective_clauses": 0,
                            "canonical": {
                                key: value
                                for key, value in canonical.items()
                                if key not in {"counter", "witness_literals"}
                            },
                            "assumptions": assumptions,
                        },
                    )
                )
            encoding_time = time.monotonic() - started
            before_stats = solver.accum_stats()
            solve_started = time.monotonic()
            satisfiable = solver.solve(assumptions=assumptions)
            solve_time = time.monotonic() - solve_started
            after_stats = solver.accum_stats()
            stats = {
                key: after_stats.get(key, 0) - before_stats.get(key, 0)
                for key in ("decisions", "conflicts", "propagations", "restarts")
            }
            result = {
                "mode": "fresh",
                "result": "SAT" if satisfiable else "UNSAT",
                "T": horizon,
                "saved_target": horizon + 2,
                "encoding_time": encoding_time,
                "solve_time": solve_time,
                "base_variables": before_canonical_vars,
                "base_clauses": before_canonical_clauses,
                "variables": encoder.vars.top,
                "clauses": encoder.clauses,
                "objective_aux_variables": 0,
                "objective_clauses": 0,
                "canonical": {key: value for key, value in canonical.items() if key not in {"counter", "witness_literals"}},
                "stats": stats,
            }
            if satisfiable:
                model = set(solver.get_model())
                schedule = [list(actions) for actions in encoder.decode(model, horizon)]
                solution = simulate(instance, 1, schedule)
                encoded_defended = {
                    vertex for vertex in range(instance.n) if encoder.d[vertex, horizon] in model
                }
                encoded_burned = {
                    vertex for vertex in range(instance.n) if encoder.b[vertex, horizon] in model
                }
                selected_witnesses = [v for v, lit in enumerate(canonical["witness_literals"]) if lit in model]
                untouched = set(range(instance.n)) - set(solution.burned) - set(solution.defended)
                if solution.k > instance.n - (horizon + 2):
                    raise AssertionError("Canonical-target model fails the requested saved target")
                if len(encoded_defended) != horizon:
                    raise AssertionError("Canonical-target SAT state does not contain exactly T defended vertices")
                if any(len(actions) != 1 for actions in schedule):
                    raise AssertionError("Canonical-target model does not select exactly one action per round")
                if encoded_burned != set(solution.burned):
                    raise AssertionError("SAT and simulator disagree on the final burned set")
                if len(untouched) < 2 or len(selected_witnesses) < 2 or not set(selected_witnesses) <= untouched:
                    raise AssertionError("Canonical-target untouched witnesses failed simulator validation")
                if solution.containment_time > horizon:
                    raise AssertionError("Canonical-target model is not contained by the requested horizon")
                result.update(
                    actual_k=solution.k,
                    saved=instance.n - solution.k,
                    containment_time=solution.containment_time,
                    defended=sorted(solution.defended),
                    encoded_defended=sorted(encoded_defended),
                    untouched=sorted(untouched),
                    untouched_witnesses=selected_witnesses,
                    encoded_schedule=schedule,
                    schedule=[list(actions) for actions in solution.schedule],
                    validated=True,
                )
            return result
        finally:
            encoder.close()


def _base_objective_query(instance, horizon, burned_bound, solver, encoder):
    encoder.ensure_horizon(horizon)
    assumptions = encoder.assumptions(horizon, burned_bound, instance.n)
    before = solver.accum_stats()
    started = time.monotonic()
    satisfiable = solver.solve(assumptions=assumptions)
    elapsed = time.monotonic() - started
    after = solver.accum_stats()
    stats = {
        key: after.get(key, 0) - before.get(key, 0)
        for key in ("decisions", "conflicts", "propagations", "restarts")
    }
    schedule = None
    solution = None
    if satisfiable:
        model = set(solver.get_model())
        schedule = [list(actions) for actions in encoder.decode(model, horizon)]
        solution = simulate(instance, encoder.firefighters, schedule)
        if solution.k > burned_bound or solution.containment_time > horizon:
            raise AssertionError(f"Replay F({horizon},{burned_bound}) failed simulator validation")
    return {
        "K": burned_bound,
        "result": "SAT" if satisfiable else "UNSAT",
        "solve_time": elapsed,
        "stats": stats,
        "actual_k": solution.k if solution else None,
        "containment_time": solution.containment_time if solution else None,
        "schedule": [list(actions) for actions in solution.schedule] if solution else schedule,
        "assumptions": assumptions,
        "variables": encoder.vars.top,
        "clauses": encoder.clauses,
    }


def _canonical_replay_worker(connection, path, firefighters, horizon, final_bound, replay_bounds, solver_name):
    solver = encoder = None
    report = {"mode": "replay", "result": "ERROR", "stages": []}
    try:
        instance = read_instance(path)
        distance, _ = preprocess(instance, firefighters)
        solver = Solver(name=solver_name)
        encoder = Encoder(instance, firefighters, solver, distance)
        encoder.ensure_horizon(horizon)
        for index, bound in enumerate([*replay_bounds, final_bound]):
            stage_name = "replay" if index < len(replay_bounds) else "final"
            assumptions = encoder.assumptions(horizon, bound, instance.n)
            if stage_name == "replay":
                connection.send(("STAGE_STARTED", {"index": index, "stage": stage_name, "K": bound}))
                stage = _base_objective_query(instance, horizon, bound, solver, encoder)
                stage.update(index=index, stage=stage_name)
                report["stages"].append(stage)
                connection.send(("STAGE_RESULT", stage))
                if stage["result"] != "SAT":
                    report.update(result="REPLAY_FAILED")
                    connection.send(("FINAL", report))
                    return
                continue

            # Extend the objective to K, snapshot it, then append the canonical
            # formulation before this one and only final solve.
            final_assumptions = assumptions
            before_vars = encoder.vars.top
            before_clauses = encoder.clauses
            canonical = _append_canonical_target(encoder, solver, horizon)
            assumptions = [encoder.h[horizon], *final_assumptions[1:], canonical["untouched_assumption"]]
            report["pre_append"] = {
                "variables": before_vars,
                "clauses": before_clauses,
                "objective_assumptions": final_assumptions,
                "assumptions": assumptions,
                "objective_aux_variables": sum(
                    objective.saved_counter.auxiliary_variables
                    if objective.saved_counter is not None
                    else 0
                    for objective in encoder.objectives.values()
                ),
                "objective_clauses": sum(
                    objective.saved_counter.number_of_clauses
                    if objective.saved_counter is not None
                    else 0
                    for objective in encoder.objectives.values()
                ),
            }
            report["canonical_append"] = {
                key: value for key, value in canonical.items()
                if key not in {"counter", "witness_literals"}
            }
            report["post_append"] = {
                "variables": encoder.vars.top,
                "clauses": encoder.clauses,
                "assumptions": assumptions,
                "objective_aux_variables": report["pre_append"]["objective_aux_variables"],
                "objective_clauses": report["pre_append"]["objective_clauses"],
            }
            connection.send(("PROGRESS", report))
            connection.send(("STAGE_STARTED", {"index": index, "stage": "final", "K": bound}))
            before = solver.accum_stats()
            solve_started = time.monotonic()
            sat = solver.solve(assumptions=assumptions)
            after = solver.accum_stats()
            stage = {
                "index": index,
                "stage": "final",
                "K": bound,
                "solve_time": time.monotonic() - solve_started,
                "stats": {
                    name: after.get(name, 0) - before.get(name, 0)
                    for name in ("decisions", "conflicts", "propagations", "restarts")
                },
                "result": "SAT" if sat else "UNSAT",
                "assumptions": assumptions,
                "variables": encoder.vars.top,
                "clauses": encoder.clauses,
            }
            if sat:
                model = set(solver.get_model())
                schedule = [list(actions) for actions in encoder.decode(model, horizon)]
                solution = simulate(instance, firefighters, schedule)
                witnesses = canonical["witness_literals"]
                chosen = [v for v, lit in enumerate(witnesses) if lit in model]
                untouched = set(range(instance.n)) - set(solution.burned) - set(solution.defended)
                encoded_defended = {
                    vertex for vertex in range(instance.n) if encoder.d[vertex, horizon] in model
                }
                encoded_burned = {
                    vertex for vertex in range(instance.n) if encoder.b[vertex, horizon] in model
                }
                if solution.k > final_bound or solution.containment_time > horizon:
                    raise AssertionError("Replay canonical-target SAT model failed simulator validation")
                if (
                    len(encoded_defended) != horizon
                    or any(len(actions) != 1 for actions in schedule)
                    or encoded_burned != set(solution.burned)
                    or len(untouched) < 2
                    or not set(chosen) <= untouched
                ):
                    raise AssertionError("Replay canonical-target model violates exact action or witness requirements")
                stage.update(
                    actual_k=solution.k,
                    saved=instance.n - solution.k,
                    containment_time=solution.containment_time,
                    defended=sorted(solution.defended),
                    encoded_defended=sorted(encoded_defended),
                    untouched=sorted(untouched),
                    untouched_witnesses=chosen,
                    encoded_schedule=schedule,
                    schedule=[list(actions) for actions in solution.schedule],
                    validated=True,
                )
            report["stages"].append(stage)
            report.update(result=stage["result"])
            connection.send(("STAGE_RESULT", stage))
            connection.send(("FINAL", report))
        connection.send(("FINAL", report))
    except Exception as exc:
        report.update(result="ERROR", error=f"{type(exc).__name__}: {exc}")
        connection.send(("ERROR", report))
    finally:
        if encoder is not None:
            encoder.close()
        if solver is not None:
            solver.delete()
        connection.close()


def _canonical_mode_worker(connection, path, horizon, solver_name):
    try:
        instance = read_instance(path)
        connection.send(("STAGE_STARTED", {"index": 0, "stage": "final", "K": instance.n - (horizon + 2)}))
        result = _canonical_target_query(instance, horizon, solver_name, connection)
        connection.send(("FINAL", result))
    except Exception as exc:
        connection.send(("ERROR", {"mode": "fresh", "result": "ERROR", "error": f"{type(exc).__name__}: {exc}"}))
    finally:
        connection.close()


def _run_mode(path, horizon, final_bound, solver_name, mode, final_timeout, replay_bounds=(), replay_timeout=90.0):
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    if mode == "fresh":
        target = _canonical_mode_worker
        args = (sender, str(path), horizon, solver_name)
    else:
        target = _canonical_replay_worker
        args = (sender, str(path), 1, horizon, final_bound, tuple(replay_bounds), solver_name)
    process = context.Process(target=target, args=args)
    started = time.monotonic()
    process.start()
    sender.close()
    report = {"mode": mode, "result": "ERROR", "stages": []}
    active = None
    active_started = None
    finished = False
    timed_out_stage = None
    try:
        while process.is_alive():
            if active is None:
                if not receiver.poll(0.05):
                    continue
                try:
                    kind, payload = receiver.recv()
                except (EOFError, OSError):
                    break
                if kind == "STAGE_STARTED":
                    active = payload
                    active_started = time.monotonic()
                elif kind == "STAGE_RESULT":
                    _merge_stage(report, payload)
                elif kind == "PROGRESS":
                    _merge_progress(report, payload)
                elif kind in {"FINAL", "ERROR"}:
                    report = payload
                    finished = True
                continue
            budget = replay_timeout if active["stage"] == "replay" else final_timeout
            remaining = budget - (time.monotonic() - active_started)
            if remaining <= 0:
                timed_out_stage = dict(active)
                process.terminate()
                process.join(0.2)
                if process.is_alive():
                    process.kill()
                    process.join(0.2)
                break
            if receiver.poll(min(0.05, remaining)):
                try:
                    kind, payload = receiver.recv()
                except (EOFError, OSError):
                    break
                if kind == "STAGE_RESULT":
                    _merge_stage(report, payload)
                    active = None
                    active_started = None
                elif kind == "PROGRESS":
                    _merge_progress(report, payload)
                elif kind in {"FINAL", "ERROR"}:
                    report = payload
                    finished = True
                    active = None
            elif not process.is_alive():
                break

        process.join(0.2)
        while receiver.poll():
            try:
                kind, payload = receiver.recv()
            except (EOFError, OSError):
                break
            if kind == "STAGE_RESULT":
                _merge_stage(report, payload)
            elif kind == "PROGRESS":
                _merge_progress(report, payload)
            elif kind in {"FINAL", "ERROR"}:
                report = payload
                finished = True
        if timed_out_stage is not None:
            timeout_result = "REPLAY_TIMEOUT" if timed_out_stage["stage"] == "replay" else "TIMEOUT"
            report["result"] = timeout_result
            report["timed_out_stage"] = timed_out_stage
            report.setdefault("stages", []).append(
                {
                    **timed_out_stage,
                    "result": "TIMEOUT",
                    "solve_time": replay_timeout if timed_out_stage["stage"] == "replay" else final_timeout,
                    "stats": None,
                    "variables": report.get("post_append", {}).get(
                        "variables", report.get("variables")
                    ),
                    "clauses": report.get("post_append", {}).get(
                        "clauses", report.get("clauses")
                    ),
                    "assumptions": report.get("post_append", {}).get(
                        "assumptions", report.get("assumptions")
                    ),
                }
            )
        elif not finished:
            report.update(result="ERROR", error=f"Worker exited with code {process.exitcode}")
        report["wall_time"] = time.monotonic() - started
        return report
    finally:
        if process.is_alive():
            process.kill()
            process.join(0.2)
        receiver.close()
        process.close()


def _merge_stage(report, stage):
    stages = report.setdefault("stages", [])
    for i, current in enumerate(stages):
        if current.get("index") == stage.get("index"):
            stages[i] = stage
            return
    stages.append(stage)
    stages.sort(key=lambda item: item.get("index", 0))


def _merge_progress(report, payload):
    stages = report.get("stages", [])
    report.update(payload)
    report["stages"] = stages


def _pair_canonicalization_regression(seed=0):
    """Compare F(T,K) with exact-T actions plus two untouched witnesses."""
    instances = _small_instances(seed)
    checked = 0
    for index, instance in enumerate(instances):
        for horizon in range(1, min(instance.n - 2, 4) + 1):
            burned_bound = instance.n - (horizon + 2)
            distance, _ = preprocess(instance, 1)
            with Solver(name="cadical300") as base_solver:
                base_encoder = Encoder(instance, 1, base_solver, distance)
                try:
                    base_encoder.ensure_horizon(horizon)
                    base_assumptions = base_encoder.assumptions(horizon, burned_bound, instance.n)
                    base_sat = base_solver.solve(assumptions=base_assumptions)
                finally:
                    base_encoder.close()
            canonical, _ = _canonical_target_query(instance, horizon, "cadical300"), None
            exact_sat = canonical["result"] == "SAT"
            if base_sat != exact_sat:
                raise AssertionError(
                    "CANONICAL_TARGET_EQUIVALENCE_FAILED: "
                    f"case={index}, n={instance.n}, B={sorted(instance.initial_fire)}, "
                    f"T={horizon}, K={burned_bound}, base={base_sat}, exact={exact_sat}"
                )
            checked += 1
    return {
        "passed": True,
        "seed": seed,
        "graph_cases": len(instances),
        "queries_checked": checked,
        "scope": "all graphs and nonempty fires through n=4; seeded n=5..6",
    }


def canonicalize_schedule_with_two_untouched(instance, firefighters, horizon, schedule):
    """Fill idle rounds from final untouched vertices while reserving two witnesses."""
    if firefighters != 1:
        raise ValueError("Schedule canonicalization currently supports one firefighter")
    padded = [list(actions) for actions in schedule[:horizon]]
    padded.extend([[] for _ in range(horizon - len(padded))])
    if any(len(actions) > 1 for actions in padded):
        raise ValueError("Input schedule exceeds one action per round")
    solution = simulate(instance, firefighters, padded)
    untouched = sorted(set(range(instance.n)) - set(solution.burned) - set(solution.defended))
    if len(solution.defended) + len(untouched) < horizon + 2 or len(untouched) < 2:
        raise ValueError("Input schedule does not meet the canonical target")
    preserved = set(untouched[:2])
    fillers = iter(vertex for vertex in untouched if vertex not in preserved)
    result = [list(actions) for actions in padded]
    for actions in result:
        if not actions:
            try:
                actions.append(next(fillers))
            except StopIteration as exc:
                raise AssertionError("Saved-count argument did not provide enough idle-slot fillers") from exc
    canonical_solution = simulate(instance, firefighters, result)
    canonical_untouched = set(range(instance.n)) - set(canonical_solution.burned) - set(canonical_solution.defended)
    if any(len(actions) != 1 for actions in result) or not preserved <= canonical_untouched:
        raise AssertionError("Schedule canonicalization failed to preserve its two untouched witnesses")
    return result


def run_canonical_target_experiment(
    path,
    firefighters,
    horizon,
    burned_bound,
    modes=("fresh", "replay"),
    replay_bounds=(991, 990),
    replay_timeout=90.0,
    final_timeout=600.0,
    solver_name="cadical300",
):
    if firefighters != 1:
        raise ValueError("canonical-target currently requires --firefighters 1")
    instance = read_instance(path)
    saved_target = instance.n - burned_bound
    if saved_target != horizon + 2:
        raise ValueError("canonical-target requires n-K = T+2")
    if any(bound <= burned_bound or bound > instance.n for bound in replay_bounds):
        raise ValueError("Every replay bound must be greater than K and at most n")
    if any(left <= right for left, right in zip(replay_bounds, replay_bounds[1:])):
        raise ValueError("Replay bounds must be strictly descending")

    regression = _pair_canonicalization_regression()

    # Special idle-fill regression: one defense contains the fire, while two
    # further rounds can be filled from untouched vertices without burning them.
    sample = Instance(
        adjacency=(frozenset({1}), frozenset({0}), frozenset(), frozenset(), frozenset(), frozenset()),
        initial_fire=frozenset({0}),
    )
    filled = canonicalize_schedule_with_two_untouched(sample, 1, 3, [[1], [], []])
    fill_solution = simulate(sample, 1, filled)
    idle_fill_regression = {
        "passed": (
            all(len(actions) == 1 for actions in filled)
            and fill_solution.k <= 1
            and len(set(range(sample.n)) - set(fill_solution.burned) - set(fill_solution.defended)) >= 2
        ),
        "input_actions": [[1], [], []],
        "canonical_schedule": filled,
        "actual_k": fill_solution.k,
        "saved": sample.n - fill_solution.k,
    }
    if not idle_fill_regression["passed"]:
        raise AssertionError("Idle-fill canonicalization regression failed")

    modes_report = {}
    for mode in modes:
        if mode not in {"fresh", "replay"}:
            raise ValueError(f"Unsupported canonical-target mode: {mode}")
        modes_report[mode] = _run_mode(
            path,
            horizon,
            burned_bound,
            solver_name,
            mode,
            final_timeout,
            replay_bounds,
            replay_timeout,
        )
    return {
        "query": {
            "T": horizon,
            "K": burned_bound,
            "saved_target": saved_target,
            "firefighters": firefighters,
        },
        "equivalence_regression": regression,
        "idle_fill_regression": idle_fill_regression,
        "modes": modes_report,
    }


def write_canonical_target_outputs(report, out_dir, stem="canonical_target"):
    output = Path(out_dir)
    output.mkdir(parents=True, exist_ok=True)
    json_path = output / f"{stem}.json"
    csv_path = output / f"{stem}.csv"
    json_path.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    fields = (
        "mode",
        "stage",
        "K",
        "result",
        "solve_time",
        "encoding_time",
        "variables",
        "clauses",
        "objective_aux_variables",
        "objective_clauses",
        "exact_action_clauses",
        "untouched_witness_variables",
        "untouched_counter_auxiliary_variables",
        "untouched_counter_clauses",
        "actual_k",
        "containment_time",
        "decisions",
        "conflicts",
        "propagations",
        "restarts",
    )
    with csv_path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for mode, mode_result in report["modes"].items():
            if mode == "fresh":
                row = {
                    "mode": mode,
                    "stage": "canonical-query",
                    "K": report["query"]["K"],
                    **mode_result,
                    **mode_result.get("canonical", {}),
                    **mode_result.get("stats", {}),
                }
                writer.writerow(row)
            else:
                for stage in mode_result.get("stages", []):
                    writer.writerow(
                        {
                            "mode": mode,
                            "stage": stage.get("stage"),
                            **stage,
                            **(stage.get("stats") or {}),
                            **mode_result.get("canonical_append", {}),
                        }
                    )
    return json_path, csv_path
