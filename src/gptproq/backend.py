"""OpenAI submission backends: Responses background mode and the Batch API.

Both build the *same* `/v1/responses` request body (model, input array, reasoning,
file_id attachments); only the submit/poll plumbing differs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openai import (
    APIConnectionError,
    APITimeoutError,
    InternalServerError,
    NotFoundError,
    OpenAI,
    RateLimitError,
)

from .models import Attachment, Job, Kind, Mode, Status

_PURPOSE = {Kind.INPUT_FILE: "user_data", Kind.INPUT_IMAGE: "vision"}


@dataclass
class Spec:
    """The assembled request, shared by both backends."""

    model: str
    reasoning_effort: str | None
    input: list[dict[str, Any]]


@dataclass
class SubmitResult:
    job: Job
    status: Status


@dataclass
class PollResult:
    status: Status
    output_text: str | None = None
    usage: dict[str, Any] | None = None
    error: str | None = None


def is_transient(exc: BaseException) -> bool:
    """Whether an exception should be retried on a later run rather than recorded."""
    if isinstance(exc, (APIConnectionError, APITimeoutError, RateLimitError, InternalServerError)):
        return True
    status = getattr(exc, "status_code", None)
    return isinstance(status, int) and (status >= 500 or status in (408, 409, 429))


def build_input(
    client: OpenAI, folder: Path, prompt_text: str, attachments: list[Attachment]
) -> list[dict[str, Any]]:
    """Assemble the Responses ``input`` array for one prompt.

    Text/code attachments are inlined directly into the prompt text (under a
    ``===== file: name =====`` header); binaries are uploaded and added as separate
    ``input_file``/``input_image`` parts. Mutates each uploaded ``Attachment``'s
    ``file_id`` so the caller can persist it.
    """
    text = prompt_text
    file_parts: list[dict[str, Any]] = []
    for att in attachments:
        path = folder / att.path
        if att.kind is Kind.INPUT_TEXT:
            body = path.read_text(encoding="utf-8").rstrip("\n")
            text += f"\n\n===== file: {att.path} =====\n\n{body}"
        else:
            if not att.file_id:
                with path.open("rb") as fh:
                    att.file_id = client.files.create(file=fh, purpose=_PURPOSE[att.kind]).id
            file_parts.append({"type": att.kind.value, "file_id": att.file_id})
    return [{"role": "user", "content": [{"type": "input_text", "text": text}, *file_parts]}]


def _request_body(spec: Spec) -> dict[str, Any]:
    body: dict[str, Any] = {"model": spec.model, "input": spec.input}
    if spec.reasoning_effort:
        body["reasoning"] = {"effort": spec.reasoning_effort}
    return body


def _extract_output_text(body: dict[str, Any]) -> str:
    text = body.get("output_text")
    if isinstance(text, str) and text:
        return text
    parts: list[str] = []
    for item in body.get("output", []):
        if item.get("type") == "message":
            for chunk in item.get("content", []):
                if chunk.get("type") == "output_text":
                    parts.append(chunk.get("text", ""))
    return "\n".join(parts)


def _response_error(resp: Any) -> str | None:
    err = getattr(resp, "error", None)
    if err is not None:
        return getattr(err, "message", None) or str(err)
    details = getattr(resp, "incomplete_details", None)
    if details is not None:
        return f"incomplete: {getattr(details, 'reason', details)}"
    return None


class BackgroundBackend:
    """Responses API background mode — per-request, polled by response id."""

    def __init__(self, client: OpenAI) -> None:
        self._client = client

    def submit(self, spec: Spec, custom_id: str) -> SubmitResult:
        resp = self._client.responses.create(background=True, store=True, **_request_body(spec))
        return SubmitResult(Job(response_id=resp.id), Status.from_api(resp.status))

    def poll(self, job: Job) -> PollResult:
        try:
            resp = self._client.responses.retrieve(job.response_id)
        except NotFoundError:
            return PollResult(Status.EXPIRED, error="response no longer retrievable (expired)")
        status = Status.from_api(resp.status)
        if status is Status.COMPLETED:
            usage = resp.usage.model_dump() if getattr(resp, "usage", None) else None
            return PollResult(status, output_text=resp.output_text, usage=usage)
        if status.is_terminal:
            return PollResult(status, error=_response_error(resp) or status.value)
        return PollResult(status)


class BatchBackend:
    """Batch API — 50% cheaper, ≤24h. One request per batch keeps the 1:1 model."""

    def __init__(self, client: OpenAI) -> None:
        self._client = client

    def submit(self, spec: Spec, custom_id: str) -> SubmitResult:
        line = {
            "custom_id": custom_id,
            "method": "POST",
            "url": "/v1/responses",
            "body": _request_body(spec),
        }
        data = (json.dumps(line) + "\n").encode("utf-8")
        upload = self._client.files.create(file=("input.jsonl", data), purpose="batch")
        batch = self._client.batches.create(
            input_file_id=upload.id, endpoint="/v1/responses", completion_window="24h"
        )
        job = Job(batch_id=batch.id, custom_id=custom_id, input_file_id=upload.id)
        return SubmitResult(job, Status.from_api(batch.status))

    def poll(self, job: Job) -> PollResult:
        batch = self._client.batches.retrieve(job.batch_id)
        status = Status.from_api(batch.status)
        if status is not Status.COMPLETED:
            if status.is_terminal:
                return PollResult(status, error=_batch_error(self._client, batch) or status.value)
            return PollResult(status)
        text = self._client.files.content(batch.output_file_id).text
        for raw in text.splitlines():
            if not raw.strip():
                continue
            rec = json.loads(raw)
            if rec.get("custom_id") != job.custom_id:
                continue
            resp = rec.get("response") or {}
            if rec.get("error") or resp.get("status_code") not in (200, None):
                return PollResult(Status.FAILED, error=json.dumps(rec.get("error") or resp)[:2000])
            body = resp.get("body") or {}
            return PollResult(
                Status.COMPLETED, output_text=_extract_output_text(body), usage=body.get("usage")
            )
        msg = f"custom_id {job.custom_id} not found in batch output"
        return PollResult(Status.FAILED, error=msg)


def _batch_error(client: OpenAI, batch: Any) -> str | None:
    error_file_id = getattr(batch, "error_file_id", None)
    if error_file_id:
        try:
            return client.files.content(error_file_id).text.strip()[:2000]
        except Exception:
            return None
    errors = getattr(batch, "errors", None)
    return str(errors) if errors else None


def make_backend(mode: Mode, client: OpenAI) -> BackgroundBackend | BatchBackend:
    return BatchBackend(client) if mode is Mode.BATCH else BackgroundBackend(client)
