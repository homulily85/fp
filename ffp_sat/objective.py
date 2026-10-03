from .totalizer import IncrementalAtMost


class IncrementalAtLeastCounter:
    """Exact prefix counts, extended by columns rather than rebuilt by threshold."""

    def __init__(self, literals, manager, add_clause, horizon=0):
        self.literals = list(literals)
        self.manager = manager
        self.add_clause = add_clause
        self.horizon = horizon
        self.maximum = 0
        self.states = {}
        self.output = {}
        self.number_of_clauses = 0
        self.auxiliary_variables = 0

    def ensure_threshold(self, q):
        n = len(self.literals)
        if not 0 <= q <= n:
            raise ValueError("Saved threshold must be between zero and the number of literals")
        for j in range(self.maximum + 1, q + 1):
            for i in range(j, n + 1):
                x = self.literals[i - 1]
                y = self.manager.new_aux(f"c[{self.horizon},{i},{j}]")
                self.auxiliary_variables += 1
                self.states[i, j] = y
                if i == 1:
                    clauses = ([-y, x], [-x, y])
                elif j == 1:
                    a = self.states[i - 1, j]
                    clauses = ([-a, y], [-x, y], [-y, a, x])
                elif j == i:
                    b = self.states[i - 1, j - 1]
                    clauses = ([-y, b], [-y, x], [-b, -x, y])
                else:
                    a, b = self.states[i - 1, j], self.states[i - 1, j - 1]
                    clauses = ([-a, y], [-b, -x, y], [-y, a, b], [-y, a, x])
                for clause in clauses:
                    self.add_clause(clause)
                    self.number_of_clauses += 1
            self.output[j] = self.states[n, j]
            self.maximum = j

    def assumption(self, q):
        self.ensure_threshold(q)
        return self.output[q] if q else None


class ObjectiveManager:
    """One lazily constructed pair of objective encodings for one horizon."""

    def __init__(self, burned, manager, add_clause, horizon):
        self.burned = list(burned)
        self.manager = manager
        self.add_clause = add_clause
        self.horizon = horizon
        self.saved_counter = None
        self.burned_totalizer = None
        self.current = None

    def assumption(self, k):
        n = len(self.burned)
        if k < 0:
            raise ValueError("Negative bound is infeasible")
        if k >= n:
            self.current = dict(
                horizon=self.horizon,
                side="none",
                encoding="tautology",
                number_of_clauses=0,
                auxiliary_variables=0,
            )
            return None
        if k > n // 2:
            if self.saved_counter is None:
                self.saved_counter = IncrementalAtLeastCounter(
                    [-b for b in self.burned],
                    self.manager,
                    lambda clause: self.add_clause(clause, "objective_saved_counter"),
                    self.horizon,
                )
            counter = self.saved_counter
            literal = counter.assumption(n - k)
            self.current = dict(
                side="saved",
                encoding="incremental_atleast_counter",
                saved_threshold=n - k,
                max_saved_threshold=counter.maximum,
            )
        else:
            if self.burned_totalizer is None:
                self.burned_totalizer = IncrementalAtMost(
                    self.burned,
                    self.manager,
                    lambda clause: self.add_clause(clause, "objective_totalizer"),
                )
            counter = self.burned_totalizer
            literal = counter.assumption(k)
            self.current = dict(side="burned", encoding="itotalizer", burned_upper_bound=k)
        self.current.update(
            horizon=self.horizon,
            number_of_clauses=counter.number_of_clauses,
            auxiliary_variables=counter.auxiliary_variables,
        )
        return literal

    def close(self):
        if self.burned_totalizer is not None:
            self.burned_totalizer.close()
