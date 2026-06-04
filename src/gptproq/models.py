"""Pydantic models and enums shared across gptproq."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

DEFAULT_MODEL = "gpt-5.5-pro"


def utcnow_iso() -> str:
    """Current UTC time as an ISO-8601 string with a trailing ``Z``."""
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


class Effort(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"


class Mode(StrEnum):
    BACKGROUND = "background"
    BATCH = "batch"


class ReasoningSummary(StrEnum):
    """How much of the model's reasoning to surface in the output."""

    AUTO = "auto"  # most detailed summary the model supports
    CONCISE = "concise"
    DETAILED = "detailed"
    NONE = "none"  # don't request a summary


class Kind(StrEnum):
    """How an attachment is delivered to the model."""

    INPUT_TEXT = "input_text"
    INPUT_FILE = "input_file"
    INPUT_IMAGE = "input_image"


DEFAULT_EFFORT = Effort.HIGH
DEFAULT_MODE = Mode.BACKGROUND
DEFAULT_SUMMARY = ReasoningSummary.AUTO


class Status(StrEnum):
    """Lifecycle status, spanning both Responses background and Batch jobs."""

    QUEUED = "queued"
    VALIDATING = "validating"
    IN_PROGRESS = "in_progress"
    FINALIZING = "finalizing"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    FAILED = "failed"
    INCOMPLETE = "incomplete"
    CANCELLED = "cancelled"
    EXPIRED = "expired"  # synthetic: response no longer retrievable

    @classmethod
    def from_api(cls, value: str) -> Status:
        """Map an API status string to a ``Status``, defaulting unknowns to ``FAILED``."""
        try:
            return cls(value)
        except ValueError:
            return cls.FAILED

    @property
    def is_terminal(self) -> bool:
        return self not in _NON_TERMINAL


_NON_TERMINAL = frozenset(
    {Status.QUEUED, Status.VALIDATING, Status.IN_PROGRESS, Status.FINALIZING, Status.CANCELLING}
)


class Attachment(BaseModel):
    path: str  # relative to the prompt folder
    sha256: str
    kind: Kind


class Job(BaseModel):
    """Backend-specific identifiers for an in-flight submission."""

    response_id: str | None = None  # background mode
    batch_id: str | None = None  # batch mode
    custom_id: str | None = None  # batch mode
    input_file_id: str | None = None  # batch mode
    output_file_id: str | None = None  # batch mode


class TaskConfig(BaseModel):
    """Per-prompt ``CONFIG.json``."""

    model: str = DEFAULT_MODEL
    reasoning_effort: Effort = DEFAULT_EFFORT
    reasoning_summary: ReasoningSummary = DEFAULT_SUMMARY
    mode: Mode = DEFAULT_MODE


class StateFile(BaseModel):
    """Per-prompt ``STATE.json``."""

    mode: Mode
    model: str
    reasoning_effort: Effort
    reasoning_summary: ReasoningSummary = DEFAULT_SUMMARY
    status: Status
    submitted_at: str
    completed_at: str | None = None
    prompt_sha256: str
    attachments: list[Attachment] = Field(default_factory=list)
    job: Job
    usage: dict[str, Any] | None = None
    gptproq_version: str | None = None
