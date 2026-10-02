class WorkTraceError(Exception):
    """Base exception for WorkTrace."""


class InvalidInputError(WorkTraceError):
    """Raised when user-provided input is invalid."""


class PreflightError(WorkTraceError):
    """Raised when the runtime environment fails preflight checks."""

    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        self.code = code


class ChatSourceError(WorkTraceError):
    """Raised when the chat source cannot provide valid data."""


class AnalyzerProtocolError(WorkTraceError):
    """Raised when analyzer input/output violates protocol constraints."""

    request_failed: bool = False


PERSONAL_RENDER_ERROR_CODES = frozenset({
    "render_schema", "render_evidence", "render_role", "render_coverage",
    "render_retention", "render_request", "render_unknown",
})


class PersonalRenderValidationError(AnalyzerProtocolError):
    """Final personal review failed with a safe, stable reason code."""

    def __init__(
        self, message: str, *, code: str, codes: tuple[str, ...] = (),
    ) -> None:
        all_codes = tuple(dict.fromkeys((code, *codes)))
        if any(item not in PERSONAL_RENDER_ERROR_CODES for item in all_codes):
            raise ValueError("Unknown personal render error code.")
        super().__init__(message)
        self.code = code
        self.codes = all_codes


class CodexProtocolViolationError(AnalyzerProtocolError):
    """Raised when Codex emits a tool or another unsupported protocol item."""


class PersonalGroupingValidationError(AnalyzerProtocolError):
    """Raised when a personal grouping result fails its task contract."""

    def __init__(self, message: str, *, partial_result: object | None = None) -> None:
        super().__init__(message)
        self.partial_result = partial_result


class DayGroupDiscoveryValidationError(AnalyzerProtocolError):
    """Raised when title-only day-group discovery violates its task contract."""


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
