"""Reconcile the queue directory against the OpenAI backends (the ``sync`` loop)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from openai import AuthenticationError, OpenAI, PermissionDeniedError

from . import __version__, store
from .backend import Spec, build_input, is_transient, make_backend
from .config import Settings
from .models import StateFile, Status, utcnow_iso
from .store import TaskState

Logger = Callable[[str], None]


@dataclass
class Summary:
    """Tally of one ``sync`` run, printed as a single cron-friendly line."""

    submitted: int = 0
    pending: int = 0
    completed: int = 0
    errors: int = 0
    deferred: int = 0

    def line(self) -> str:
        text = (
            f"submitted={self.submitted} pending={self.pending} "
            f"completed={self.completed} errors={self.errors}"
        )
        if self.deferred:
            text += f" deferred={self.deferred}"
        return text


def run_sync(
    settings: Settings, queue: Path, *, verbose: bool = False, log: Logger = print
) -> Summary:
    if not settings.api_key:
        raise SystemExit("No API key set. Run `gptproq config set api_key <token>`.")
    client = OpenAI(api_key=settings.api_key)
    summary = Summary()

    for folder in store.prompt_dirs(queue):
        name = folder.name
        try:
            state = store.classify(folder)
            if state is TaskState.DONE:
                _warn_on_drift(folder, log)
            elif state is TaskState.FAILED:
                if verbose:
                    log(f"  {name}: failed (see {store.ERROR})")
            elif state is TaskState.RUNNING:
                _poll_one(client, folder, summary, log, verbose=verbose)
            else:  # NEW or READY — configure if needed, then submit, in one pass
                if state is TaskState.NEW:
                    # NEW -> READY: seed CONFIG.json from the global defaults.
                    store.write_config(folder, settings.task_defaults())
                _submit_one(client, folder, summary, log)  # READY -> RUNNING
        except (AuthenticationError, PermissionDeniedError) as exc:
            raise SystemExit(
                f"Authentication failed ({exc}). Check `gptproq config get api_key`."
            ) from exc
        except Exception as exc:
            # Isolate failures to one prompt; transient errors retry next run.
            if is_transient(exc):
                summary.deferred += 1
                log(f"  {name}: transient error ({type(exc).__name__}); retrying next run")
            else:
                store.write_error(folder, f"{type(exc).__name__}: {exc}")
                summary.errors += 1
                log(f"  {name}: ERROR ({type(exc).__name__}); wrote {store.ERROR}")

    log(summary.line())
    return summary


def _submit_one(client: OpenAI, folder: Path, summary: Summary, log: Logger) -> None:
    cfg = store.read_config(folder)
    prompt_text = store.read_prompt(folder)
    if not prompt_text.strip():
        log(f"  {folder.name}: empty {store.PROMPT}, skipping")
        return

    attachments = store.gather_attachments(folder)
    input_arr = build_input(client, folder, prompt_text, attachments)
    spec = Spec(model=cfg.model, reasoning_effort=cfg.reasoning_effort.value, input=input_arr)

    result = make_backend(cfg.mode, client).submit(spec, _custom_id(folder.name))

    state = StateFile(
        mode=cfg.mode,
        model=cfg.model,
        reasoning_effort=cfg.reasoning_effort,
        status=result.status,
        submitted_at=utcnow_iso(),
        prompt_sha256=store.sha256_text(prompt_text),
        attachments=attachments,
        job=result.job,
        gptproq_version=__version__,
    )
    store.write_state(folder, state)
    summary.submitted += 1
    job_id = result.job.batch_id or result.job.response_id
    log(f"  {folder.name}: submitted [{cfg.mode}] ({job_id})")


def _poll_one(
    client: OpenAI, folder: Path, summary: Summary, log: Logger, *, verbose: bool
) -> None:
    state = store.read_state(folder)
    result = make_backend(state.mode, client).poll(state.job)
    state.status = result.status

    if result.status is Status.COMPLETED:
        store.write_output(folder, result.output_text or "")
        state.completed_at = utcnow_iso()
        state.usage = result.usage
        store.write_state(folder, state)
        summary.completed += 1
        log(f"  {folder.name}: completed -> {store.OUTPUT}")
    elif result.status.is_terminal:
        store.write_state(folder, state)
        store.write_error(folder, result.error or result.status.value)
        summary.errors += 1
        log(f"  {folder.name}: {result.status.value}; wrote {store.ERROR}")
    else:
        store.write_state(folder, state)
        summary.pending += 1
        if verbose:
            log(f"  {folder.name}: {result.status.value}")


def _warn_on_drift(folder: Path, log: Logger) -> None:
    try:
        state = store.read_state(folder)
    except FileNotFoundError:
        return
    drift = store.sha256_text(store.read_prompt(folder)) != state.prompt_sha256 or {
        a.path: a.sha256 for a in store.gather_attachments(folder)
    } != {a.path: a.sha256 for a in state.attachments}
    if drift:
        log(f"  {folder.name}: inputs changed since answer; delete OUTPUT.md/STATE.json to rerun")


def _custom_id(name: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in name)
    return safe[:60] or "task"
