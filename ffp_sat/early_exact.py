"""Containment-time proof and early exact-action diagnostic helpers."""

from __future__ import annotations

import multiprocessing
import random
import time
from itertools import combinations

from pysat.solvers import Solver

from .encoder import Encoder
from .instance import Instance, read_instance
from .preprocess import preprocess
from .simulator import simulate


def append_early_exact_actions(encoder, solver, min_containment_time, exact_range="through"):
    """Append one positive-action clause per requested early round, idempotently."""
    if solver is not encoder.solver:
        raise ValueError("EARLY-EXACT clauses must be added to the encoder's solver")
    if encoder.firefighters != 1:
        raise ValueError("EARLY-EXACT diagnostic currently supports exactly one firefighter")
    if exact_range not in {"before", "through"}:
        raise ValueError("exact_range must be 'before' or 'through'")
    if min_containment_time < 0:
        raise ValueError("Minimum containment time must be non-negative")
    stop = min_containment_time + (exact_range == "through")
    requested = tuple(range(1, stop))
    if requested and max(requested) > encoder.horizon:
        raise ValueError("EARLY-EXACT requires the requested rounds to be encoded")

    started = time.monotonic()
    added_rounds = set(getattr(encoder, "_early_exact_added_rounds", set()))
    before_top = encoder.vars.top
    before_clauses = encoder.clauses
    added = []
    for round_number in requested:
        if round_number in added_rounds:
            continue
        encoder.add(
            [encoder.a[vertex, round_number] for vertex in range(encoder.instance.n)]
        )
        added_rounds.add(round_number)
        added.append(round_number)
    encoder._early_exact_added_rounds = added_rounds
    return {
        "rounds": list(requested),
        "added_rounds": added,
        "variables_added": encoder.vars.top - before_top,
        "clauses_added": encoder.clauses - before_clauses,
        "append_time": time.monotonic() - started,
    }


def _containment_worker(connection, path, firefighters, solver_name, horizon):
    solver = encoder = None
    result = {"horizon": horizon, "result": "ERROR"}
    try:
        instance = read_instance(path)
        distance, _ = preprocess(instance, firefighters)
        solver = Solver(name=solver_name)
        encoder = Encoder(instance, firefighters, solver, distance)
        encode_started = time.monotonic()
        encoder.ensure_horizon(horizon)
        activation = encoder.ensure_containment(horizon)
        assumptions = [activation]
        result.update(
            encoding_time=time.monotonic() - encode_started,
            variables=encoder.vars.top,
            clauses=encoder.clauses,
            assumptions=assumptions,
            objective_counters=bool(encoder.objectives),
            objective_auxiliary_variables=0,
        )
        if encoder.objectives:
            raise AssertionError("Containment-only encoding unexpectedly created an objective")
        connection.send(("READY", result))
        before = solver.accum_stats() if hasattr(solver, "accum_stats") else {}
        started = time.monotonic()
        satisfiable = solver.solve(assumptions=assumptions)
        result["solve_time"] = time.monotonic() - started
        after = solver.accum_stats() if hasattr(solver, "accum_stats") else {}
        result["stats"] = {
            key: after.get(key, 0) - before.get(key, 0)
            for key in ("decisions", "conflicts", "propagations", "restarts")
        }
        if satisfiable:
            model = set(solver.get_model())
            schedule = [list(actions) for actions in encoder.decode(model, horizon)]
            solution = simulate(instance, firefighters, schedule)
            if solution.containment_time > horizon:
                raise AssertionError("Containment-only SAT model failed simulator validation")
            result.update(
                result="SAT",
                actual_containment_time=solution.containment_time,
                burned_count=solution.k,
                schedule=[list(actions) for actions in solution.schedule],
            )
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


def run_containment_query(path, firefighters, solver_name, horizon, timeout, setup_timeout=60.0):
    """Run a fresh, objective-free C(horizon) query with a hard parent timeout."""
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(
        target=_containment_worker,
        args=(sender, str(path), firefighters, solver_name, horizon),
    )
    started = time.monotonic()
    process.start()
    sender.close()
    latest = {"horizon": horizon, "result": "ERROR"}
    ready_at = None
    completed = False
    timed_out = False
    try:
        while process.is_alive():
            now = time.monotonic()
            deadline = setup_timeout if ready_at is None else timeout
            origin = started if ready_at is None else ready_at
            remaining = deadline - (now - origin)
            if remaining <= 0:
                timed_out = True
                break
            if not receiver.poll(min(0.05, remaining)):
                continue
            try:
                kind, payload = receiver.recv()
            except (EOFError, OSError):
                break
            latest = payload
            if kind == "READY":
                ready_at = time.monotonic()
            elif kind == "RESULT":
                completed = True
        if timed_out:
            process.terminate()
            process.join(0.2)
            if process.is_alive():
                process.kill()
                process.join(0.2)
            latest.update(
                result="TIMEOUT",
                timeout_seconds=timeout if ready_at is not None else setup_timeout,
                timed_out_phase="solve" if ready_at is not None else "setup",
            )
        else:
            process.join(0.2)
        while receiver.poll():
            try:
                kind, payload = receiver.recv()
            except (EOFError, OSError):
                break
            latest = payload
            if kind == "RESULT":
                completed = True
        if not completed and not timed_out:
            latest.update(result="ERROR", error=f"Containment worker exited with code {process.exitcode}")
        latest["worker_wall_time"] = time.monotonic() - started
        return latest
    finally:
        if process.is_alive():
            process.kill()
            process.join(0.2)
        receiver.close()
        process.close()


def _is_contained(instance, burned, defended):
    return not any(
        neighbor not in burned and neighbor not in defended
        for vertex in burned
        for neighbor in instance.adjacency[vertex]
    )


def brute_force_containment_time(instance):
    """Exact D=1 containment-time oracle over all legal schedules."""
    burned0 = frozenset(instance.initial_fire)
    defended0 = frozenset()
    if _is_contained(instance, burned0, defended0):
        return 0
    frontier = {(burned0, defended0)}
    for round_number in range(1, instance.n + 2):
        next_frontier = set()
        for burned, defended in frontier:
            untouched = set(range(instance.n)) - burned - defended
            for selected in [None, *sorted(untouched)]:
                new_defended = defended if selected is None else defended | {selected}
                burning_neighbors = {
                    neighbor
                    for vertex in burned
                    for neighbor in instance.adjacency[vertex]
                }
                new_burned = burned | (burning_neighbors - new_defended)
                if _is_contained(instance, new_burned, new_defended):
                    return round_number
                next_frontier.add((frozenset(new_burned), frozenset(new_defended)))
        frontier = next_frontier
    raise AssertionError("Exhaustive D=1 search did not contain the fire")


def brute_force_feasible(instance, horizon, bound, exact_rounds=()):
    """Return whether a legal D=1 schedule satisfies F(T,K) and exact rounds."""
    burned0 = frozenset(instance.initial_fire)
    defended0 = frozenset()
    if _is_contained(instance, burned0, defended0):
        return len(burned0) <= bound
    frontier = {(burned0, defended0)}
    exact_rounds = set(exact_rounds)
    for round_number in range(1, horizon + 1):
        next_frontier = set()
        for burned, defended in frontier:
            untouched = set(range(instance.n)) - burned - defended
            choices = sorted(untouched) if round_number in exact_rounds else [None, *sorted(untouched)]
            for selected in choices:
                new_defended = defended if selected is None else defended | {selected}
                burning_neighbors = {
                    neighbor
                    for vertex in burned
                    for neighbor in instance.adjacency[vertex]
                }
                new_burned = burned | (burning_neighbors - new_defended)
                if len(new_burned) > bound:
                    continue
                if _is_contained(instance, new_burned, new_defended):
                    return True
                next_frontier.add((frozenset(new_burned), frozenset(new_defended)))
        frontier = next_frontier
    return False


def _make_small_instance(n, edges, initial_fire):
    adjacency = [set() for _ in range(n)]
    for u, v in edges:
        adjacency[u].add(v)
        adjacency[v].add(u)
    return Instance(tuple(frozenset(row) for row in adjacency), frozenset(initial_fire))


def _sat_ffp(instance, horizon, bound, exact_rounds=()):
    distance, _ = preprocess(instance, 1)
    with Solver(name="cadical300") as solver:
        encoder = Encoder(instance, 1, solver, distance)
        try:
            encoder.ensure_horizon(horizon)
            assumptions = encoder.assumptions(horizon, bound, instance.n)
            if exact_rounds is not None:
                for round_number in exact_rounds:
                    encoder.add([encoder.a[v, round_number] for v in range(instance.n)])
            return solver.solve(assumptions=assumptions)
        finally:
            encoder.close()


def run_small_graph_equivalence_regression(seed=0):
    """Brute-force and SAT-check BASE/EARLY-EXACT equivalence on small graphs."""
    rng = random.Random(seed)
    cases = []
    for n in range(1, 4):
        edges = list(combinations(range(n), 2))
        for mask in range(1 << len(edges)):
            graph_edges = [edge for index, edge in enumerate(edges) if mask & (1 << index)]
            for initial_mask in range(1, 1 << n):
                initial = [v for v in range(n) if initial_mask & (1 << v)]
                cases.append(_make_small_instance(n, graph_edges, initial))
    for n in range(4, 7):
        for _ in range(8):
            edges = [
                edge for edge in combinations(range(n), 2)
                if rng.random() < 0.35
            ]
            initial = [v for v in range(n) if rng.random() < 0.3]
            if not initial:
                initial = [rng.randrange(n)]
            cases.append(_make_small_instance(n, edges, initial))

    checked_queries = 0
    for case_index, instance in enumerate(cases):
        t_min = brute_force_containment_time(instance)
        for horizon in range(t_min, instance.n + 1):
            through = tuple(range(1, min(t_min, horizon) + 1))
            before = tuple(range(1, min(t_min, horizon + 1)))
            for bound in range(len(instance.initial_fire), instance.n + 1):
                expected = brute_force_feasible(instance, horizon, bound)
                base = _sat_ffp(instance, horizon, bound)
                exact_through = _sat_ffp(instance, horizon, bound, through)
                exact_before = _sat_ffp(instance, horizon, bound, before)
                if not (base == expected == exact_through == exact_before):
                    raise AssertionError(
                        "EARLY-EXACT equivalence counterexample: "
                        f"case={case_index}, n={instance.n}, edges={instance.adjacency}, "
                        f"B={sorted(instance.initial_fire)}, Tmin={t_min}, T={horizon}, "
                        f"K={bound}, oracle={expected}, base={base}, "
                        f"through={exact_through}, before={exact_before}"
                    )
                checked_queries += 1
    # Explicit case: C(1) is impossible, C(2) is feasible. With no defenses,
    # fire first advances down two star branches and contains in round two.
    last_round = _make_small_instance(
        7,
        [(0, 1), (0, 2), (0, 3), (1, 4), (2, 5), (3, 6)],
        [0],
    )
    last_t_min = brute_force_containment_time(last_round)
    if last_t_min != 2:
        raise AssertionError(f"Last-round regression expected Tmin=2, got {last_t_min}")
    for bound in range(1, last_round.n + 1):
        base = _sat_ffp(last_round, 2, bound)
        exact = _sat_ffp(last_round, 2, bound, (1, 2))
        expected = brute_force_feasible(last_round, 2, bound)
        if not (base == exact == expected):
            raise AssertionError(f"Last-containment-round EARLY-EXACT mismatch at K={bound}")
        checked_queries += 1
    return {
        "passed": True,
        "seed": seed,
        "graph_cases": len(cases) + 1,
        "queries_checked": checked_queries,
        "last_containment_round_case": {"T_min": last_t_min, "passed": True},
        "scope": "D=1, exhaustive n<=3 plus seeded n=4..6 and explicit Tmin=2 idle-final case",
    }


def run_early_exact_experiment(
    path,
    firefighters,
    solver_name,
    horizon,
    bound,
    known_upper,
    containment_timeout=120.0,
    replay_bounds=(991, 990),
    replay_timeout=90.0,
    final_timeout=600.0,
    exact_range="through",
    upper_schedule=None,
):
    if firefighters != 1:
        raise ValueError("early-action-exact currently requires --firefighters 1")
    if known_upper < 0 or known_upper > horizon:
        raise ValueError("Known containment upper bound must lie in [0, T]")
    instance = read_instance(path)
    small_graph = run_small_graph_equivalence_regression()
    report = {
        "query": {"T": horizon, "K": bound},
        "config": {
            "instance": str(path),
            "firefighters": firefighters,
            "solver": solver_name,
            "known_containment_upper": known_upper,
            "containment_query_time": containment_timeout,
            "replay_bounds": list(replay_bounds),
            "replay_query_time": replay_timeout,
            "final_query_time": final_timeout,
            "exact_range": exact_range,
        },
        "small_graph_equivalence": small_graph,
        "containment_search": {
            "known_upper": known_upper,
            "queries": [],
            "min_containment_time": None,
            "proved": False,
            "upper_source": "provided_schedule" if upper_schedule is not None else "containment_sat_query",
        },
        "early_exact": {},
        "master_comparison": {},
    }

    witness = None
    if upper_schedule is not None:
        solution = simulate(instance, firefighters, upper_schedule)
        if solution.containment_time > known_upper:
            raise ValueError(
                f"Upper schedule contains at {solution.containment_time}, later than declared {known_upper}"
            )
        witness = {
            "schedule": [list(actions) for actions in solution.schedule],
            "containment_time": solution.containment_time,
        }
    else:
        upper_query = run_containment_query(
            path, firefighters, solver_name, known_upper, containment_timeout
        )
        report["containment_search"]["queries"].append(upper_query)
        if upper_query["result"] != "SAT":
            report["result"] = "CONTAINMENT_MIN_UNKNOWN"
            report["containment_search"]["reason"] = "Could not certify the supplied containment upper bound"
            return report
        witness = {
            "schedule": upper_query["schedule"],
            "containment_time": upper_query["actual_containment_time"],
        }

    best_upper = witness["containment_time"]
    while best_upper > 0:
        query_horizon = best_upper - 1
        query = run_containment_query(
            path, firefighters, solver_name, query_horizon, containment_timeout
        )
        report["containment_search"]["queries"].append(query)
        print(
            f"containment-only C({query_horizon})={query['result']} "
            f"time={query.get('solve_time', query.get('worker_wall_time', 0.0)):.3f}s",
            flush=True,
        )
        if query["result"] == "UNSAT":
            report["containment_search"].update(
                min_containment_time=best_upper,
                proved=True,
                upper_witness=witness,
            )
            break
        if query["result"] == "TIMEOUT":
            report["result"] = "CONTAINMENT_MIN_UNKNOWN"
            report["containment_search"]["reason"] = f"C({query_horizon}) timed out"
            return report
        if query["result"] != "SAT":
            report["result"] = "CONTAINMENT_QUERY_ERROR"
            report["containment_search"]["reason"] = query.get("error", query["result"])
            return report
        actual = query["actual_containment_time"]
        if actual >= best_upper:
            raise AssertionError("A SAT query below the known upper failed to improve its witness")
        best_upper = actual
        witness = {"schedule": query["schedule"], "containment_time": actual}
    else:
        report["containment_search"].update(
            min_containment_time=0,
            proved=True,
            upper_witness=witness,
        )

    t_min = report["containment_search"]["min_containment_time"]
    rounds = list(range(1, t_min + (exact_range == "through")))
    report["early_exact"] = {
        "range": exact_range,
        "rounds": rounds,
        "variables_added": 0,
        "clauses_added": len(rounds),
        "small_graph_equivalence_passed": small_graph["passed"],
    }
    if any(bound_item <= bound or bound_item > instance.n for bound_item in replay_bounds):
        raise ValueError("Replay bounds must be greater than final K and at most n")

    from .prefix_core_mining import run_master_replay

    base = run_master_replay(
        path, firefighters, solver_name, horizon, bound, replay_bounds,
        "base", (), replay_timeout, final_timeout,
    )
    report["master_comparison"]["base"] = base
    if base.get("pre_append_signature") is None:
        report["result"] = "MASTER_BASE_FAILED"
        report["reason"] = base.get("result")
        return report
    exact = run_master_replay(
        path, firefighters, solver_name, horizon, bound, replay_bounds,
        "early-exact", (), replay_timeout, final_timeout,
        expected_history=base["pre_append_signature"],
        early_exact_minimum=t_min,
        early_exact_range=exact_range,
    )
    exact["history_matches_base"] = (
        exact.get("pre_append_signature") == base["pre_append_signature"]
        and exact.get("result") != "HISTORY_MISMATCH"
    )
    if exact.get("result") == "UNSAT":
        exact["unsat_interpretation"] = (
            "UNSAT via brute-force-tested existence-preserving EARLY-EXACT restriction"
        )
    report["master_comparison"]["early-exact"] = exact
    report["result"] = "HISTORY_MISMATCH" if not exact["history_matches_base"] else "COMPLETED"
    final_stage = next(
        (row for row in exact.get("stages", []) if row.get("stage") == "final"), None
    )
    if final_stage and final_stage.get("result") == "SAT":
        import json
        from pathlib import Path

        Path("diagnostics").mkdir(exist_ok=True)
        Path("diagnostics/early_exact_solution.json").write_text(
            json.dumps(
                {
                    "instance": str(path),
                    "T": horizon,
                    "K_bound": bound,
                    "actual_k": final_stage["actual_k"],
                    "containment_time": final_stage["containment_time"],
                    "schedule": final_stage["schedule"],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    return report
