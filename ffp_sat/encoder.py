from .totalizer import AtMost
from .variables import VarManager


class Encoder:
    def __init__(self, instance, firefighters, solver, distance):
        self.instance, self.firefighters, self.solver = instance, firefighters, solver
        self.distance = distance
        self.vars = VarManager()
        self.b, self.d, self.a, self.h, self.objectives = {}, {}, {}, {}, {}
        self.horizon = 0
        self.clauses = 0
        self.extensions = 0
        self.trees = []
        for v in range(instance.n):
            self.b[v, 0], self.d[v, 0] = self.vars.new(), self.vars.new()
            self.add([self.b[v, 0] if v in instance.initial_fire else -self.b[v, 0]])
            self.add([-self.d[v, 0]])

    def add(self, clause):
        self.solver.add_clause(clause)
        self.clauses += 1

    def ensure_horizon(self, target):
        if target <= self.horizon:
            return
        self.extensions += 1
        for t in range(self.horizon + 1, target + 1):
            for v in range(self.instance.n):
                self.b[v, t], self.d[v, t], self.a[v, t] = self.vars.new(), self.vars.new(), self.vars.new()
            for v in range(self.instance.n):
                b, bp, d, dp, a = self.b[v, t], self.b[v, t - 1], self.d[v, t], self.d[v, t - 1], self.a[v, t]
                for clause in ([-bp, b], [-dp, d], [-b, -d], [-a, d], [-a, -dp], [-d, dp, a]):
                    self.add(clause)
                for u in sorted(self.instance.adjacency[v]):
                    self.add([-self.b[u, t - 1], d, b])
                self.add([-b, bp] + [self.b[u, t - 1] for u in sorted(self.instance.adjacency[v])])
                if t < self.distance[v]:
                    self.add([-b])
            tree = AtMost([self.a[v, t] for v in range(self.instance.n)], self.firefighters, self.vars)
            self.trees.append(tree)
            for clause in tree.clauses:
                self.add(clause)
            bound = tree.assumption(self.firefighters)
            if bound is not None:
                self.add([bound])
            self.h[t] = self.vars.new(activation=True)
            for v in range(self.instance.n):
                self.add([-self.h[t], -self.b[v, t], self.b[v, t - 1]])
        self.horizon = target

    def assumptions(self, t, k, upper):
        if t not in self.objectives:
            tree = AtMost([self.b[v, t] for v in range(self.instance.n)], upper, self.vars)
            self.objectives[t] = tree
            self.trees.append(tree)
            for clause in tree.clauses:
                self.add(clause)
        bound = self.objectives[t].assumption(k)
        return [self.h[t]] + ([bound] if bound is not None else [])

    def decode(self, model, t):
        positive = {v for v in model if v > 0}
        return [
            tuple(v for v in range(self.instance.n) if self.a[v, step] in positive)
            for step in range(1, t + 1)
        ]

    def stats(self):
        return dict(
            n_semantic_vars=self.vars.semantic,
            n_activation_vars=self.vars.activation,
            n_aux_vars=self.vars.auxiliary,
            n_clauses=self.clauses,
            number_of_horizon_extensions=self.extensions,
        )

    def close(self):
        for tree in self.trees:
            tree.close()
