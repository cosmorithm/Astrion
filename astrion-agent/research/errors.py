"""Stable business error codes, compatible with existing ValueError boundaries."""


class ResearchError(ValueError):
    def __init__(self, detail):
        super().__init__(detail)
        self.code = detail.split(":", 1)[0]
