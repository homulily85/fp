from functools import lru_cache
from itertools import combinations

from ffp_sat.instance import Instance


def graph(n, edges, fire=(0,)):
    adjacency = [set() for _ in range(n)]
    for u, v in edges:
        adjacency[u].add(v)
        adjacency[v].add(u)
    return Instance(tuple(map(frozenset, adjacency)), frozenset(fire))


def brute_force(instance, firefighters, horizon=None):
    # Independent bitmask dynamics; does not call the production simulator.
    adjacency = [sum(1 << u for u in neighbors) for neighbors in instance.adjacency]
    all_vertices = (1 << instance.n) - 1

    @lru_cache(None)
    def visit(burned, defended, remaining):
        neighbors = 0
        for v in range(instance.n):
            if (burned >> v) & 1:
                neighbors |= adjacency[v]
        if not (neighbors & (all_vertices ^ (burned | defended))):
            return burned.bit_count()
        if remaining == 0:
            return instance.n + 1
        untouched = [v for v in range(instance.n) if not ((burned | defended) >> v) & 1]
        best = instance.n + 1
        for count in range(min(firefighters, len(untouched)) + 1):
            for actions in combinations(untouched, count):
                protected = defended | sum(1 << v for v in actions)
                spread = neighbors & (all_vertices ^ (burned | protected))
                best = min(best, visit(burned | spread, protected, remaining - 1))
        return best

    initial = sum(1 << v for v in instance.initial_fire)
    return visit(initial, 0, horizon if horizon is not None else instance.n + 1)


def statistics():
    return dict(
        encoding_time=0.0,
        sat_time=0.0,
        sat_calls=0,
        sat_results=0,
        unsat_results=0,
        number_of_incumbent_improvements=0,
    )
