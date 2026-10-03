from pysat.card import ITotalizer


class AtMost:
    def __init__(self, literals, maximum, manager):
        self.size = len(literals)
        self.maximum = min(maximum, self.size - 1)
        self.tree = None
        self.clauses = []
        if self.size and self.maximum >= 0:
            self.tree = ITotalizer(lits=literals, ubound=self.maximum, top_id=manager.top)
            self.clauses = self.tree.cnf.clauses
            manager.reserve(self.tree.top_id)

    def assumption(self, k):
        if k < 0:
            raise ValueError("Negative bound is infeasible")
        if k >= self.size:
            return None
        if self.tree is None or k > self.maximum:
            raise ValueError("Bound not encoded")
        return -self.tree.rhs[k]

    def close(self):
        if self.tree is not None:
            self.tree.delete()
            self.tree = None


class IncrementalAtMost:
    """Objective-only totalizer; add just the delta when increasing its capacity."""

    def __init__(self, literals, manager, add_clause):
        self.literals = list(literals)
        self.manager = manager
        self.add_clause = add_clause
        self.tree = None
        self.maximum = -1
        self.number_of_clauses = 0
        self.auxiliary_variables = 0

    def ensure_bound(self, k):
        if k < 0:
            raise ValueError("Negative bound is infeasible")
        if k >= len(self.literals) or k <= self.maximum:
            return
        previous_top = self.manager.top
        previous_count = 0 if self.tree is None else len(self.tree.cnf.clauses)
        if self.tree is None:
            self.tree = ITotalizer(lits=self.literals, ubound=k, top_id=self.manager.top)
        else:
            self.tree.increase(ubound=k, top_id=self.manager.top)
        self.manager.reserve(max(self.manager.top, self.tree.top_id))
        self.auxiliary_variables += self.manager.top - previous_top
        for clause in self.tree.cnf.clauses[previous_count:]:
            self.add_clause(clause)
            self.number_of_clauses += 1
        self.maximum = k

    def assumption(self, k):
        self.ensure_bound(k)
        if k >= len(self.literals):
            return None
        return -self.tree.rhs[k]

    def close(self):
        if self.tree is not None:
            self.tree.delete()
            self.tree = None
