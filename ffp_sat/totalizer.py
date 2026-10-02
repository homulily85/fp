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
