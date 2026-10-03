"""Phase preferences for isolated FFP diagnostic queries."""

import random
from collections import Counter
from enum import Enum

from .heuristic import threat
from .simulator import simulate


class PhaseMode(str, Enum):
    NONE = "none"
    ACTION = "action"
    FULL = "full"
    CONSENSUS = "consensus"


def trace_schedule(instance, firefighters, schedule, horizon):
    """Return simulator states for rounds 0..horizon, extending containment."""
    solution = simulate(instance, firefighters, schedule)
    if solution.containment_time > horizon:
        raise ValueError("Incumbent schedule is not contained by the requested horizon")

    burned = set(instance.initial_fire)
    defended = set()
    trace = [{"burned": frozenset(burned), "defended": frozenset(defended), "actions": frozenset()}]
    for round_number in range(1, horizon + 1):
        actions = set(solution.schedule[round_number - 1]) if round_number <= len(solution.schedule) else set()
        defended.update(actions)
        spread = set().union(*(instance.adjacency[v] for v in burned)) - burned - defended
        burned.update(spread)
        trace.append(
            {"burned": frozenset(burned), "defended": frozenset(defended), "actions": frozenset(actions)}
        )

    if trace[solution.containment_time]["burned"] != solution.burned:
        raise AssertionError("Trace burned state differs from simulator")
    if trace[solution.containment_time]["defended"] != solution.defended:
        raise AssertionError("Trace defended state differs from simulator")
    return trace


def action_phases(encoder, schedule, horizon):
    selected = {
        (vertex, round_number)
        for round_number, actions in enumerate(schedule, start=1)
        for vertex in actions
    }
    return [
        encoder.a[vertex, round_number]
        if (vertex, round_number) in selected
        else -encoder.a[vertex, round_number]
        for round_number in range(1, horizon + 1)
        for vertex in range(encoder.instance.n)
    ]


def full_phases(encoder, instance, firefighters, schedule, horizon):
    trace = trace_schedule(instance, firefighters, schedule, horizon)
    phases = []
    for round_number in range(1, horizon + 1):
        state = trace[round_number]
        for vertex in range(instance.n):
            for variables, selected in (
                (encoder.a, state["actions"]),
                (encoder.b, state["burned"]),
                (encoder.d, state["defended"]),
            ):
                variable = variables[vertex, round_number]
                phases.append(variable if vertex in selected else -variable)
    return phases


def build_consensus_pool(instance, firefighters, supplied_schedules, pool_size, seed):
    """Validate, deduplicate, rank, and deterministically supplement schedules."""
    unique = {}

    def add(schedule):
        solution = simulate(instance, firefighters, schedule)
        normalized = tuple(tuple(actions) for actions in solution.schedule)
        unique[normalized] = solution

    for schedule in supplied_schedules:
        add(schedule)

    rng = random.Random(seed)
    attempts = 0
    while len(unique) < pool_size and attempts < max(100, pool_size * 100):
        attempts += 1
        add(threat(instance, firefighters, mode="random", rng=rng).schedule)

    ranked = sorted(
        unique.values(),
        key=lambda solution: (solution.k, solution.containment_time, solution.schedule),
    )
    return ranked[:pool_size], {
        "generated": attempts,
        "unique": len(unique),
        "retained": min(pool_size, len(ranked)),
        "best_k": ranked[0].k if ranked else None,
        "worst_k": ranked[-1].k if ranked else None,
        "median_k": ranked[len(ranked) // 2].k if ranked else None,
    }


def consensus_phases(encoder, schedules, horizon):
    n = encoder.instance.n
    phases = []
    votes = []
    for round_number in range(1, horizon + 1):
        counts = Counter(
            vertex
            for actions in schedules
            if round_number <= len(actions)
            for vertex in actions[round_number - 1]
        )
        selected = {
            vertex
            for vertex, _ in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[
                : encoder.firefighters
            ]
        }
        for vertex in range(n):
            phases.append(encoder.a[vertex, round_number] if vertex in selected else -encoder.a[vertex, round_number])
        votes.append(
            {
                "round": round_number,
                "vertices": [
                    {"vertex": vertex, "votes": counts[vertex], "fraction": counts[vertex] / len(schedules)}
                    for vertex in sorted(selected)
                ],
            }
        )
    return phases, votes


def build_phase_literals(
    encoder,
    instance,
    firefighters,
    horizon,
    mode,
    incumbent_schedule=None,
    consensus_schedules=None,
):
    mode = PhaseMode(mode)
    if mode is PhaseMode.NONE:
        return [], []
    if mode is PhaseMode.ACTION:
        if incumbent_schedule is None:
            raise ValueError("ACTION guidance requires an incumbent schedule")
        return action_phases(encoder, incumbent_schedule, horizon), []
    if mode is PhaseMode.FULL:
        if incumbent_schedule is None:
            raise ValueError("FULL guidance requires an incumbent schedule")
        return full_phases(encoder, instance, firefighters, incumbent_schedule, horizon), []
    if not consensus_schedules:
        raise ValueError("CONSENSUS guidance requires at least one validated schedule")
    return consensus_phases(encoder, consensus_schedules, horizon)


def validate_phase_literals(phases):
    variables = [abs(literal) for literal in phases]
    if len(set(variables)) != len(variables):
        raise ValueError("A phase variable appears more than once")
    return {
        "phase_literals": len(phases),
        "phase_positive": sum(literal > 0 for literal in phases),
        "phase_negative": sum(literal < 0 for literal in phases),
    }
