class WorkTraceError(Exception):
    """Base exception for WorkTrace."""


class InvalidInputError(WorkTraceError):
    """Raised when user-provided input is invalid."""


class PreflightError(WorkTraceError):
    """Raised when the runtime environment fails preflight checks."""


class ChatSourceError(WorkTraceError):
    """Raised when the chat source cannot provide valid data."""


class AnalyzerProtocolError(WorkTraceError):
    """Raised when analyzer input/output violates protocol constraints."""


class RetryableAnalyzerProtocolError(AnalyzerProtocolError):
    """Raised when retrying the same analyzer request may succeed."""


class StoreWriteError(WorkTraceError):
    """Raised when store write or validation fails."""


class DeliveryError(WorkTraceError):
    """Raised when self delivery fails."""
