import random
import time

from .simulator import simulate


def threat(instance, firefighters, mode="id", rng=None):
    burned, defended, schedule = set(instance.initial_fire), set(), []
    def contained():
        return not (set().union(*(instance.adjacency[v] for v in burned)) - burned - defended)

    if contained():
        return simulate(instance, firefighters, schedule)
    while True:
        candidates = sorted(set().union(*(instance.adjacency[v] for v in burned)) - burned - defended)
        if mode == "degree":
            candidates.sort(key=lambda v: (-len(instance.adjacency[v]), v))
        if mode == "random" and len(candidates) > firefighters:
            actions = rng.sample(candidates, firefighters)
        else:
            actions = candidates[:firefighters]
        schedule.append(actions)
        defended.update(actions)
        spread = set(candidates) - defended
        burned.update(spread)
        if contained():
            return simulate(instance, firefighters, schedule)


def dominates(left, right):
    """Whether (T_left, K_left) Pareto-dominates (T_right, K_right)."""
    return (
        left.containment_time <= right.containment_time
        and left.k <= right.k
        and (left.containment_time < right.containment_time or left.k < right.k)
    )


def portfolio(instance, firefighters, initial, deadline, seed, on_improvement):
    rng = random.Random(seed)
    best, frontier = initial, [initial]
    mode = "degree"
    while time.monotonic() < deadline:
        candidate = threat(instance, firefighters, mode, rng)
        mode = "random"
        pair = (candidate.containment_time, candidate.k)
        if not any(
            (s.containment_time, s.k) == pair or dominates(s, candidate) for s in frontier
        ):
            frontier = [
                s
                for s in frontier
                if (s.containment_time, s.k) != pair and not dominates(candidate, s)
            ]
            frontier.append(candidate)
        if (candidate.k, candidate.containment_time, candidate.schedule) < (
            best.k,
            best.containment_time,
            best.schedule,
        ):
            best = candidate
            on_improvement(best)
    return best, frontier
