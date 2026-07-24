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


class PersonalGroupingValidationError(AnalyzerProtocolError):
    """Raised when a personal grouping result fails its task contract."""

    def __init__(self, message: str, *, partial_result: object | None = None) -> None:
        super().__init__(message)
        self.partial_result = partial_result


class RetryableAnalyzerProtocolError(AnalyzerProtocolError):
    """Raised when retrying the same analyzer request may succeed."""


class ModelInputLimitError(AnalyzerProtocolError):
    """Raised when a request violates input packing or provider input limits."""


class ModelInputRejectedError(ModelInputLimitError):
    """Raised when the model service rejects a request as invalid input."""


class StoreWriteError(WorkTraceError):
    """Raised when store write or validation fails."""


class DeliveryError(WorkTraceError):
    """Raised when self delivery fails."""
