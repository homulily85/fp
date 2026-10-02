from collections import deque
from math import inf


def preprocess(instance, firefighters):
    distance = [inf] * instance.n
    queue = deque(instance.initial_fire)
    for v in queue:
        distance[v] = 0
    while queue:
        u = queue.popleft()
        for v in instance.adjacency[u]:
            if distance[v] == inf:
                distance[v] = distance[u] + 1
                queue.append(v)
    threatened = set().union(*(instance.adjacency[v] for v in instance.initial_fire)) - instance.initial_fire
    lower = len(instance.initial_fire) + max(0, len(threatened) - firefighters)
    return distance, lower
