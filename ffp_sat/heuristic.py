import random
import time

from .simulator import simulate


def threat(instance, firefighters, mode="id", rng=None):
    burned, defended, schedule = set(instance.initial_fire), set(), []
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
        if not spread:
            return simulate(instance, firefighters, schedule)
        burned.update(spread)


def portfolio(instance, firefighters, initial, deadline, seed, on_improvement):
    rng = random.Random(seed)
    best, frontier = initial, [initial]
    mode = "degree"
    while time.monotonic() < deadline:
        candidate = threat(instance, firefighters, mode, rng)
        mode = "random"
        pair = (candidate.containment_time, candidate.k)
        if not any(s.containment_time <= pair[0] and s.k <= pair[1] for s in frontier):
            frontier = [s for s in frontier if not (pair[0] <= s.containment_time and pair[1] <= s.k)]
            frontier.append(candidate)
        if (candidate.k, candidate.containment_time, candidate.schedule) < (
            best.k,
            best.containment_time,
            best.schedule,
        ):
            best = candidate
            on_improvement(best)
    return best, frontier
