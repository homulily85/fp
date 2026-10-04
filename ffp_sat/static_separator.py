"""Static vertex-separator relaxation diagnostic for the firefighter problem."""

from __future__ import annotations

import csv
import json
import multiprocessing
import random
import time
from collections import deque
from itertools import combinations
from pathlib import Path

from pysat.solvers import Solver

from .encoder import Encoder
from .instance import Instance, read_instance
from .objective import IncrementalAtLeastCounter
from .preprocess import preprocess
from .simulator import simulate
from .totalizer import AtMost
from .variables import VarManager


def _reachable_without(instance, removed):
    reachable = set(instance.initial_fire) - set(removed)
    queue = deque(reachable)
    while queue:
        vertex = queue.popleft()
        for neighbor in instance.adjacency[vertex]:
            if neighbor not in removed and neighbor not in reachable:
                reachable.add(neighbor)
                queue.append(neighbor)
    return reachable


def validate_static_witness(instance, defense_budget, saved_target, separator, safe_region):
    separator = set(separator)
    safe_region = set(safe_region)
    initial = set(instance.initial_fire)
    if separator & initial:
        raise AssertionError("Static separator contains an initial burning vertex")
    if safe_region & initial:
        raise AssertionError("Static safe region contains an initial burning vertex")
    if separator & safe_region:
        raise AssertionError("Static separator and safe region overlap")
    if len(separator) > defense_budget:
        raise AssertionError("Static separator exceeds the defense budget")
    if len(separator) + len(safe_region) < saved_target:
        raise AssertionError("Static witness does not meet the requested saved target")
    reachable = _reachable_without(instance, separator)
    if safe_region & reachable:
        raise AssertionError("A claimed safe vertex remains reachable from the initial fire")
    canonical_safe = set(range(instance.n)) - separator - reachable
    return {
        "separator": sorted(separator),
        "safe_region": sorted(safe_region),
        "canonical_safe_region": sorted(canonical_safe),
        "separator_size": len(separator),
        "model_safe_size": len(safe_region),
        "canonical_safe_size": len(canonical_safe),
        "static_saved": len(separator) + len(canonical_safe),
        "reachable_after_separator": sorted(reachable),
        "validated": True,
    }


def brute_static_optimum(instance, defense_budget):
    available = [vertex for vertex in range(instance.n) if vertex not in instance.initial_fire]
    best = 0
    best_separator = set()
    for size in range(min(defense_budget, len(available)) + 1):
        for choice in combinations(available, size):
            separator = set(choice)
            reachable = _reachable_without(instance, separator)
            saved = len(separator) + instance.n - len(separator | reachable)
            if saved > best:
                best = saved
                best_separator = separator
    return best, sorted(best_separator)


def _build_static_formula(instance, defense_budget, saved_target, solver):
    manager = VarManager()
    separator = [manager.new(f"r[{vertex}]") for vertex in range(instance.n)]
    safe = [manager.new(f"s[{vertex}]") for vertex in range(instance.n)]
    clause_count = 0

    def add(clause):
        nonlocal clause_count
        solver.add_clause(list(clause))
        clause_count += 1

    for vertex in instance.initial_fire:
        add([-separator[vertex]])
        add([-safe[vertex]])
    for vertex in range(instance.n):
        add([-separator[vertex], -safe[vertex]])
    for u, neighbors in enumerate(instance.adjacency):
        for v in neighbors:
            if u < v:
                add([-safe[u], safe[v], separator[v]])
                add([-safe[v], safe[u], separator[u]])

    cardinality_clauses = 0
    defense_tree = None
    defense_assumption = None
    if defense_budget < instance.n:
        defense_tree = AtMost(separator, defense_budget, manager)
        for clause in defense_tree.clauses:
            add(clause)
        cardinality_clauses += len(defense_tree.clauses)
        defense_assumption = defense_tree.assumption(defense_budget)

    saved_counter = IncrementalAtLeastCounter([*separator, *safe], manager, add, horizon="static_saved")
    saved_assumption = None
    if saved_target > 0:
        saved_assumption = saved_counter.assumption(saved_target)
    assumptions = [literal for literal in (defense_assumption, saved_assumption) if literal is not None]
    return {
        "manager": manager,
        "separator_vars": separator,
        "safe_vars": safe,
        "assumptions": assumptions,
        "base_clause_count": clause_count - cardinality_clauses - saved_counter.number_of_clauses,
        "clause_count": clause_count,
        "cardinality_clauses": cardinality_clauses + saved_counter.number_of_clauses,
        "saved_counter": saved_counter,
        "defense_tree": defense_tree,
    }


def _decode_static_model(solver, formula, instance, defense_budget, saved_target):
    positive = {literal for literal in solver.get_model() if literal > 0}
    separator = [vertex for vertex, literal in enumerate(formula["separator_vars"]) if literal in positive]
    safe = [vertex for vertex, literal in enumerate(formula["safe_vars"]) if literal in positive]
    return validate_static_witness(instance, defense_budget, saved_target, separator, safe)


def _static_solver_worker(connection, path, defense_budget, saved_target, solver_name):
    solver = formula = None
    result = {
        "target": saved_target,
        "defense_budget": defense_budget,
        "result": "ERROR",
    }
    try:
        instance = read_instance(path)
        solver = Solver(name=solver_name)
        started = time.monotonic()
        formula = _build_static_formula(instance, defense_budget, saved_target, solver)
        result.update(
            encoding_time=time.monotonic() - started,
            semantic_variables=2 * instance.n,
            auxiliary_variables=formula["manager"].auxiliary,
            total_variables=formula["manager"].top,
            clauses=formula["clause_count"],
            base_clauses=formula["base_clause_count"],
            cardinality_clauses=formula["cardinality_clauses"],
            assumptions=formula["assumptions"],
        )
        connection.send(("READY", result))
        before = solver.accum_stats()
        solve_started = time.monotonic()
        satisfiable = solver.solve(assumptions=formula["assumptions"])
        result["solve_time"] = time.monotonic() - solve_started
        after = solver.accum_stats()
        result["stats"] = {
            key: after.get(key, 0) - before.get(key, 0)
            for key in ("decisions", "conflicts", "propagations", "restarts")
        }
        if satisfiable:
            witness = _decode_static_model(solver, formula, instance, defense_budget, saved_target)
            result.update(result="SAT", witness=witness)
        else:
            result.update(
                result="UNSAT",
                implies_dynamic_unsat_at_horizon=(defense_budget, saved_target),
            )
        connection.send(("RESULT", result))
    except Exception as exc:
        result.update(result="ERROR", error=f"{type(exc).__name__}: {exc}")
        connection.send(("RESULT", result))
    finally:
        if formula and formula.get("defense_tree") is not None:
            formula["defense_tree"].close()
        if solver is not None:
            solver.delete()
        connection.close()


def run_static_separator(path, firefighters, horizon, burned_bound, solver_name, time_limit=600.0):
    """Run the static relaxation in an isolated process with a hard wall-clock limit."""
    if firefighters < 1 or horizon < 0 or burned_bound < 0:
        raise ValueError("Invalid static separator query")
    instance = read_instance(path)
    defense_budget = horizon * firefighters
    saved_target = instance.n - burned_bound
    if burned_bound > instance.n:
        raise ValueError("Burned bound exceeds the graph order")
    if saved_target < 0:
        raise ValueError("Saved target must be non-negative")
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(
        target=_static_solver_worker,
        args=(sender, str(path), defense_budget, saved_target, solver_name),
    )
    process.start()
    sender.close()
    latest = {"target": saved_target, "defense_budget": defense_budget, "result": "ERROR"}
    started = time.monotonic()
    completed = False
    try:
        timed_out = False
        while True:
            remaining = time_limit - (time.monotonic() - started)
            if remaining <= 0:
                timed_out = process.is_alive() and not completed
                break
            if not receiver.poll(min(0.05, remaining)):
                if not process.is_alive():
                    break
                continue
            try:
                kind, payload = receiver.recv()
            except (EOFError, OSError):
                if not process.is_alive():
                    break
                if completed:
                    process.join(0.1)
                    if not process.is_alive():
                        break
                continue
            latest = payload
            if kind == "RESULT":
                completed = True
                # A completed SAT/UNSAT result is authoritative. Give the worker
                # a short grace period to close its solver and pipe cleanly.
                process.join(0.1)
                if not process.is_alive():
                    break
        timed_out = timed_out or (process.is_alive() and not completed)
        if timed_out:
            process.terminate()
            process.join(0.5)
            if process.is_alive():
                process.kill()
                process.join(0.5)
        else:
            process.join(0.5)
        while receiver.poll():
            try:
                kind, payload = receiver.recv()
            except (EOFError, OSError):
                break
            latest = payload
            completed = completed or kind == "RESULT"
        if timed_out:
            latest["result"] = "TIMEOUT"
        elif not completed:
            latest.update(result="ERROR", error=f"Static solver worker exited with code {process.exitcode}")
        latest["wall_time"] = time.monotonic() - started
        latest["time_limit"] = time_limit
        return latest
    finally:
        if process.is_alive():
            process.kill()
            process.join(0.5)
        receiver.close()
        process.close()


def _static_query(instance, defense_budget, saved_target):
    with Solver(name="cadical300") as solver:
        formula = _build_static_formula(instance, defense_budget, saved_target, solver)
        try:
            satisfiable = solver.solve(assumptions=formula["assumptions"])
            witness = (
                _decode_static_model(solver, formula, instance, defense_budget, saved_target)
                if satisfiable
                else None
            )
            return satisfiable, witness
        finally:
            if formula["defense_tree"] is not None:
                formula["defense_tree"].close()


def run_static_small_graph_regression(seed=0):
    """Check static SAT against exact separator enumeration and dynamic implication."""
    rng = random.Random(seed)
    cases = []
    for n in range(1, 4):
        edges = list(combinations(range(n), 2))
        for mask in range(1 << len(edges)):
            graph_edges = [edge for index, edge in enumerate(edges) if mask & (1 << index)]
            for initial_mask in range(1, 1 << n):
                initial = [vertex for vertex in range(n) if initial_mask & (1 << vertex)]
                adjacency = [set() for _ in range(n)]
                for u, v in graph_edges:
                    adjacency[u].add(v)
                    adjacency[v].add(u)
                cases.append(Instance(tuple(map(frozenset, adjacency)), frozenset(initial)))
    for n in range(4, 8):
        for _ in range(5):
            edges = [edge for edge in combinations(range(n), 2) if rng.random() < 0.35]
            initial = {vertex for vertex in range(n) if rng.random() < 0.25}
            if not initial:
                initial.add(rng.randrange(n))
            adjacency = [set() for _ in range(n)]
            for u, v in edges:
                adjacency[u].add(v)
                adjacency[v].add(u)
            cases.append(Instance(tuple(map(frozenset, adjacency)), frozenset(initial)))

    static_queries = 0
    dynamic_implications = 0
    for index, instance in enumerate(cases):
        budget = min(3, instance.n)
        optimum, _ = brute_static_optimum(instance, budget)
        for target in range(instance.n + 1):
            satisfiable, _ = _static_query(instance, budget, target)
            if satisfiable != (optimum >= target):
                raise AssertionError(
                    f"Static CNF mismatch case={index}, n={instance.n}, "
                    f"B={sorted(instance.initial_fire)}, budget={budget}, "
                    f"target={target}, optimum={optimum}, SAT={satisfiable}"
                )
            static_queries += 1

        distance, _ = preprocess(instance, 1)
        for horizon in range(1, min(3, instance.n) + 1):
            for burned_bound in range(len(instance.initial_fire), instance.n + 1):
                with Solver(name="cadical300") as dynamic_solver:
                    encoder = Encoder(instance, 1, dynamic_solver, distance)
                    try:
                        encoder.ensure_horizon(horizon)
                        assumptions = encoder.assumptions(horizon, burned_bound, instance.n)
                        dynamic_sat = dynamic_solver.solve(assumptions=assumptions)
                    finally:
                        encoder.close()
                if dynamic_sat:
                    static_sat, _ = _static_query(instance, horizon, instance.n - burned_bound)
                    if not static_sat:
                        raise AssertionError(
                            f"Dynamic-to-static implication failed case={index}, "
                            f"T={horizon}, K={burned_bound}"
                        )
                    dynamic_implications += 1
    return {
        "passed": True,
        "seed": seed,
        "graph_cases": len(cases),
        "static_queries_checked": static_queries,
        "dynamic_implications_checked": dynamic_implications,
        "scope": "all graphs and nonempty fires through n=3; seeded n=4..7",
    }


def validate_incumbent_static_witness(instance, firefighters, horizon, schedule):
    solution = simulate(instance, firefighters, schedule)
    untouched = set(range(instance.n)) - set(solution.burned) - set(solution.defended)
    witness = validate_static_witness(
        instance,
        horizon * firefighters,
        len(solution.defended) + len(untouched),
        solution.defended,
        untouched,
    )
    witness.update(
        dynamic_k=solution.k,
        dynamic_containment_time=solution.containment_time,
        dynamic_saved=len(solution.defended) + len(untouched),
    )
    return witness


def run_static_separator_experiment(path, firefighters, horizon, burned_bound, solver_name, time_limit=600.0):
    instance = read_instance(path)
    if firefighters < 1:
        raise ValueError("Static separator diagnostic requires at least one firefighter")
    incumbent_schedule = [[568], [546], [599], [67], [732], [269]]
    incumbent = validate_incumbent_static_witness(instance, firefighters, horizon, incumbent_schedule)
    if incumbent["dynamic_saved"] != 10:
        raise AssertionError(
            "The known regression incumbent should save exactly 10 vertices; "
            f"got {incumbent['dynamic_saved']}"
        )

    small_graph = run_static_small_graph_regression()
    target10 = run_static_separator(
        path,
        firefighters,
        horizon,
        instance.n - 10,
        solver_name,
        min(60.0, time_limit),
    )
    if target10["result"] != "SAT":
        raise AssertionError(
            "Static regression query for the known 10-saved incumbent was not SAT: "
            f"{target10}"
        )

    target = run_static_separator(path, firefighters, horizon, burned_bound, solver_name, time_limit)
    target["implies_dynamic_unsat_at_horizon"] = horizon if target["result"] == "UNSAT" else None
    return {
        "query": {
            "T": horizon,
            "K": burned_bound,
            "defense_budget": horizon * firefighters,
            "saved_target": instance.n - burned_bound,
        },
        "small_graph_regression": small_graph,
        "incumbent_regression": incumbent,
        "target_10_regression": target10,
        "target_query": target,
        "result": target["result"],
    }


def write_static_outputs(report, out_dir, stem="static_separator"):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"{stem}.json"
    json_path.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    csv_path = out_dir / f"{stem}.csv"
    fields = (
        "target",
        "result",
        "solve_time",
        "separator_size",
        "model_safe_size",
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
        row = dict(report)
        witness = report.get("witness", {})
        row.update(witness)
        row.update(report.get("stats", {}))
        writer.writerow(row)
    return json_path, csv_path
