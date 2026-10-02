class VarManager:
    def __init__(self):
        self.top = 0
        self.semantic = 0
        self.activation = 0
        self.auxiliary = 0

    def new(self, activation=False):
        self.top += 1
        if activation:
            self.activation += 1
        else:
            self.semantic += 1
        return self.top

    def reserve(self, top):
        if top < self.top:
            raise AssertionError("Variable IDs must increase")
        self.auxiliary += top - self.top
        self.top = top
