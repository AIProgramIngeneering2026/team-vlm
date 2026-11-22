from .errors import DomainError, UnknownOperation


class Registry:
    def __init__(self):
        self._tools = {}

    def register(self, tool):
        if tool.name in self._tools:
            raise DomainError('duplicate_operation', 'Duplicate tool name', operation=tool.name)
        self._tools[tool.name] = tool

    def get(self, name):
        try:
            return self._tools[name]
        except KeyError:
            raise UnknownOperation(name, self.names()) from None

    def names(self):
        return sorted(self._tools)
