"""A failed source is different from a successful empty response."""


class SourceError(RuntimeError):
    def __init__(self, message, *, source='', kind='transport'):
        super().__init__(f'{source}: {message}' if source else message)
        self.source = source
        self.kind = kind
