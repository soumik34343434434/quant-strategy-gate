"""Every error here means BLOCKED for whatever depended on it. None means pass."""


class GateError(Exception):
    pass


class InputError(GateError):
    """Report data is missing, malformed or inconsistent."""


class MetricError(GateError):
    """A metric cannot be computed from the data (zero variance, no losers, ruin...)."""


class ConfigError(GateError):
    """The config or its registry failed verification."""
