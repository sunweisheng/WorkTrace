from __future__ import annotations

from enum import Enum


class DailyRunStatus(str, Enum):
    SUCCESS = "success"
    SUCCESS_WITH_WARNINGS = "success_with_warnings"
    FAILED = "failed"
    INVALID_INPUT = "invalid_input"


class ContextRequestType(str, Enum):
    EARLIER_MESSAGES = "earlier_messages"
    LATER_MESSAGES = "later_messages"
    ATTACHMENT_TEXT = "attachment_text"
    LINKED_FILE_TEXT = "linked_file_text"


class ContextDirection(str, Enum):
    EARLIER = "earlier"
    LATER = "later"


class LinkType(str, Enum):
    FEISHU_DOC = "feishu_doc"
    NORMAL = "normal"


class AnchorStatus(str, Enum):
    PENDING = "pending"
    COMPLETED = "completed"
    NEEDS_MORE_CONTEXT = "needs_more_context"
    NEEDS_ATTACHMENT_TEXT = "needs_attachment_text"
    NOT_WORK_RELATED = "not_work_related"
    UNCERTAIN = "uncertain"
    FAILED = "failed"
    SKIPPED = "skipped"
