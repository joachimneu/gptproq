"""OpenAI submission backends: Responses background mode and the Batch API.

Both build the same `/v1/responses` request body (model, input array, reasoning).
Attachments are sent **inline** — text is folded into the prompt and binaries are
base64 `input_file`/`input_image` parts — so background mode creates no stored
Files at all. The Batch API *must* upload its request as a file and yields result
files; those are deleted in `poll` once the answer has been read.
"""

from __future__ import annotations

import base64
import json
import mimetypes
import uuid
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


@dataclass
class Spec:
    """The assembled request, shared by both backends."""

    model: str
    reasoning_effort: str | None
    reasoning_summary: str | None
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
    reasoning: str | None = None  # reasoning summary, when requested/available


def is_transient(exc: BaseException) -> bool:
    """Whether an exception should be retried on a later run rather than recorded."""
    if isinstance(exc, (APIConnectionError, APITimeoutError, RateLimitError, InternalServerError)):
        return True
    status = getattr(exc, "status_code", None)
    return isinstance(status, int) and (status >= 500 or status in (408, 409, 429))


def _data_url(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def build_input(
    folder: Path, prompt_text: str, attachments: list[Attachment]
) -> list[dict[str, Any]]:
    """Assemble the Responses ``input`` array. Text/code attachments are inlined into
    the prompt; binaries are sent inline as base64 ``input_file``/``input_image`` parts
    (nothing is uploaded to the Files API)."""
    text = prompt_text
    parts: list[dict[str, Any]] = []
    for att in attachments:
        path = folder / att.path
        if att.kind is Kind.INPUT_TEXT:
            body = path.read_text(encoding="utf-8").rstrip("\n")
            text += f"\n\n===== file: {att.path} =====\n\n{body}"
        elif att.kind is Kind.INPUT_IMAGE:
            parts.append({"type": "input_image", "image_url": _data_url(path)})
        else:  # INPUT_FILE
            parts.append({"type": "input_file", "filename": att.path, "file_data": _data_url(path)})
    return [{"role": "user", "content": [{"type": "input_text", "text": text}, *parts]}]


def _request_body(spec: Spec) -> dict[str, Any]:
    body: dict[str, Any] = {"model": spec.model, "input": spec.input}
    reasoning: dict[str, str] = {}
    if spec.reasoning_effort:
        reasoning["effort"] = spec.reasoning_effort
    if spec.reasoning_summary and spec.reasoning_summary != "none":
        reasoning["summary"] = spec.reasoning_summary
    if reasoning:
        body["reasoning"] = reasoning
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


def _extract_reasoning(body: dict[str, Any]) -> str:
    """Collect any reasoning-summary text from the response's ``reasoning`` items."""
    parts: list[str] = []
    for item in body.get("output", []):
        if item.get("type") == "reasoning":
            for chunk in item.get("summary", []):
                text = chunk.get("text")
                if text:
                    parts.append(text)
    return "\n\n".join(parts)


def _response_error(resp: Any) -> str | None:
    err = getattr(resp, "error", None)
    if err is not None:
        return getattr(err, "message", None) or str(err)
    details = getattr(resp, "incomplete_details", None)
    if details is not None:
        return f"incomplete: {getattr(details, 'reason', details)}"
    return None


class BackgroundBackend:
    """Responses API background mode — per-request, polled by id. Creates no Files."""

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
            body = resp.model_dump()
            usage = resp.usage.model_dump() if getattr(resp, "usage", None) else None
            return PollResult(
                status,
                output_text=resp.output_text,
                usage=usage,
                reasoning=_extract_reasoning(body) or None,
            )
        if status.is_terminal:
            return PollResult(status, error=_response_error(resp) or status.value)
        return PollResult(status)


class BatchBackend:
    """Batch API — 50% cheaper, ≤24h. Uploads a uniquely-named JSONL request and yields
    result files; all of them are deleted (by owned id) once the answer is read."""

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
        # Unique + human-legible: clearly this tool, which prompt, and safe to delete.
        filename = f"gptproq-{custom_id}-{uuid.uuid4().hex}.jsonl"
        upload = self._client.files.create(file=(filename, data), purpose="batch")
        batch = self._client.batches.create(
            input_file_id=upload.id,
            endpoint="/v1/responses",
            completion_window="24h",
            metadata={"tool": "gptproq", "prompt": custom_id},
        )
        job = Job(batch_id=batch.id, custom_id=custom_id, input_file_id=upload.id)
        return SubmitResult(job, Status.from_api(batch.status))

    def poll(self, job: Job) -> PollResult:
        batch = self._client.batches.retrieve(job.batch_id)
        status = Status.from_api(batch.status)
        if status is not Status.COMPLETED:
            if status.is_terminal:
                err = _batch_error(self._client, batch) or status.value
                self._cleanup(job, batch)
                return PollResult(status, error=err)
            return PollResult(status)
        if not batch.output_file_id:
            self._cleanup(job, batch)
            return PollResult(Status.FAILED, error="batch completed without an output file")
        text = self._client.files.content(batch.output_file_id).text
        result = _parse_batch_output(text, job.custom_id)
        self._cleanup(job, batch)
        return result

    def _cleanup(self, job: Job, batch: Any) -> None:
        """Delete the input/output/error files we created or own for this batch (by id)."""
        ids = [
            job.input_file_id,
            getattr(batch, "output_file_id", None),
            getattr(batch, "error_file_id", None),
        ]
        for file_id in ids:
            if file_id:
                try:
                    self._client.files.delete(file_id)
                except Exception:
                    pass  # best-effort; never let cleanup mask the result


def _parse_batch_output(text: str, custom_id: str) -> PollResult:
    for raw in text.splitlines():
        if not raw.strip():
            continue
        rec = json.loads(raw)
        if rec.get("custom_id") != custom_id:
            continue
        resp = rec.get("response") or {}
        if rec.get("error") or resp.get("status_code") not in (200, None):
            return PollResult(Status.FAILED, error=json.dumps(rec.get("error") or resp)[:2000])
        body = resp.get("body") or {}
        return PollResult(
            Status.COMPLETED,
            output_text=_extract_output_text(body),
            usage=body.get("usage"),
            reasoning=_extract_reasoning(body) or None,
        )
    return PollResult(Status.FAILED, error=f"custom_id {custom_id} not found in batch output")


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
