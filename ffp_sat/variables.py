class VarManager:
    def __init__(self, capture_names=False):
        self.top = 0
        self.semantic = 0
        self.activation = 0
        self.auxiliary = 0
        self.names = {} if capture_names else None

    def new(self, name=None, activation=False):
        self.top += 1
        if activation:
            self.activation += 1
        else:
            self.semantic += 1
        if self.names is not None:
            if name is None:
                name = f"activation_{self.top}" if activation else f"semantic_{self.top}"
            self.names[self.top] = name
        return self.top

    def reserve(self, top):
        if top < self.top:
            raise AssertionError("Variable IDs must increase")
        previous = self.top
        self.auxiliary += top - previous
        self.top = top
        if self.names is not None:
            for variable in range(previous + 1, top + 1):
                self.names[variable] = f"totalizer_aux_{variable}"

    def new_aux(self, name=None):
        self.top += 1
        self.auxiliary += 1
        if self.names is not None:
            self.names[self.top] = name or f"aux_{self.top}"
        return self.top
