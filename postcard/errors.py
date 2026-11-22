class DomainError(Exception):
    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code, self.message, self.details = code, message, details


class InvalidTransition(DomainError):
    def __init__(self, state, command):
        super().__init__('invalid_transition', 'Transition rejected', state=state, command=command)


class InvalidParams(DomainError):
    def __init__(self, field, reason):
        super().__init__('invalid_params', reason, field=field, reason=reason)


class UnknownOperation(DomainError):
    def __init__(self, name, available):
        super().__init__('unknown_operation', 'Unknown operation', operation=name, available=available)


class ToolExecutionError(DomainError):
    pass
