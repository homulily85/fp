from dataclasses import dataclass


@dataclass(frozen=True)
class Solution:
    schedule: tuple[tuple[int, ...], ...]
    burned: frozenset[int]
    defended: frozenset[int]
    containment_time: int

    @property
    def k(self):
        return len(self.burned)


def simulate(instance, firefighters, schedule):
    if firefighters < 1:
        raise ValueError("Firefighters must be positive")
    burned, defended = set(instance.initial_fire), set()
    normalized = []
    for t in range(1, instance.n + 2):
        actions = tuple(schedule[t - 1]) if t <= len(schedule) else ()
        if len(actions) > firefighters or len(set(actions)) != len(actions):
            raise ValueError("Invalid number of defense actions")
        if any(v < 0 or v >= instance.n or v in burned or v in defended for v in actions):
            raise ValueError("Defense requires an untouched vertex")
        defended.update(actions)
        normalized.append(tuple(sorted(actions)))
        spread = set().union(*(instance.adjacency[v] for v in burned)) - burned - defended
        if not spread:
            return Solution(tuple(normalized), frozenset(burned), frozenset(defended), t)
        burned.update(spread)
    raise AssertionError("Fire did not terminate")
