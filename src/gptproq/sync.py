"""Reconcile the queue directory against the OpenAI backends (the ``sync`` loop).

The per-prompt state machine is explicit. For each prompt, ``_drive`` repeatedly:
``classify`` the current state from the magic files present → call that state's
handler → the handler performs the transition and returns whether the prompt
**advanced** to a state that is actionable right now (loop again) or is now
**waiting on the API / terminal** (stop until the next ``sync``). Adding a state
means adding an enum member, a handler, and a ``_HANDLERS`` entry — nothing else
makes assumptions about the ordering.
"""

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


@dataclass
class _Ctx:
    client: OpenAI
    settings: Settings
    summary: Summary
    log: Logger
    verbose: bool


def run_sync(
    settings: Settings, queue: Path, *, verbose: bool = False, log: Logger = print
) -> Summary:
    folders = store.prompt_dirs(queue)
    if not folders:
        log(f"no prompt folders in {queue}")
        return Summary()

    log(f"{'before:':<9}{_census_line(folders)}")
    if not settings.api_key:
        raise SystemExit("No API key set. Run `gptproq config set api_key <token>`.")
    ctx = _Ctx(OpenAI(api_key=settings.api_key), settings, Summary(), log, verbose)

    for folder in folders:
        try:
            _drive(ctx, folder)
        except (AuthenticationError, PermissionDeniedError) as exc:
            raise SystemExit(
                f"Authentication failed ({exc}). Check `gptproq config get api_key`."
            ) from exc
        except Exception as exc:
            # Isolate failures to one prompt; transient errors retry next run.
            if is_transient(exc):
                ctx.summary.deferred += 1
                log(f"  {folder.name}: transient error ({type(exc).__name__}); retrying next run")
            else:
                store.write_error(folder, f"{type(exc).__name__}: {exc}")
                ctx.summary.errors += 1
                log(f"  {folder.name}: ERROR ({type(exc).__name__}); wrote {store.ERROR}")

    log(f"{'changes:':<9}{ctx.summary.line()}")
    log(f"{'after:':<9}{_census_line(folders)}")
    return ctx.summary


def _drive(ctx: _Ctx, folder: Path) -> None:
    """Advance one prompt until it waits on the API or reaches a terminal state."""
    for _ in range(len(TaskState)):  # bounded: each step advances at most one state
        advanced = _HANDLERS[store.classify(folder)](ctx, folder)
        if not advanced:
            return


# --- State transition handlers ----------------------------------------------------
# Each handler performs the work for its state and returns True if the prompt
# advanced to a state we can act on again right now (loop), or False if it is now
# waiting on the API / is terminal (stop until the next sync).


def _on_new(ctx: _Ctx, folder: Path) -> bool:
    # NEW (only PROMPT.md) -> READY: seed CONFIG.json from the global defaults.
    store.write_config(folder, ctx.settings.task_defaults())
    return True  # READY is actionable right now


def _on_ready(ctx: _Ctx, folder: Path) -> bool:
    # READY (PROMPT.md + CONFIG.json) -> RUNNING: submit the job.
    cfg = store.read_config(folder)
    prompt_text = store.read_prompt(folder)
    if not prompt_text.strip():
        ctx.log(f"  {folder.name}: empty {store.PROMPT}, skipping")
        return False
    attachments = store.gather_attachments(folder)
    spec = Spec(
        model=cfg.model,
        reasoning_effort=cfg.reasoning_effort.value,
        reasoning_summary=cfg.reasoning_summary.value,
        reasoning_mode=cfg.reasoning_mode.value,
        input=build_input(folder, prompt_text, attachments),
    )
    result = make_backend(cfg.mode, ctx.client).submit(spec, _custom_id(folder.name))
    store.write_state(
        folder,
        StateFile(
            mode=cfg.mode,
            model=cfg.model,
            reasoning_effort=cfg.reasoning_effort,
            reasoning_summary=cfg.reasoning_summary,
            reasoning_mode=cfg.reasoning_mode,
            status=result.status,
            submitted_at=utcnow_iso(),
            prompt_sha256=store.sha256_text(prompt_text),
            attachments=attachments,
            job=result.job,
            gptproq_version=__version__,
        ),
    )
    ctx.summary.submitted += 1
    job_id = result.job.batch_id or result.job.response_id
    ctx.log(f"  {folder.name}: submitted [{cfg.mode}] ({job_id})")
    return False  # now waiting on the API


def _on_running(ctx: _Ctx, folder: Path) -> bool:
    # RUNNING (+ STATE.json) -> poll: stays RUNNING, or becomes DONE / FAILED.
    state = store.read_state(folder)
    result = make_backend(state.mode, ctx.client).poll(state.job)
    state.status = result.status
    if result.status is Status.COMPLETED:
        store.write_output(folder, _compose_output(result.output_text, result.reasoning))
        state.completed_at = utcnow_iso()
        state.usage = result.usage
        store.write_state(folder, state)
        ctx.summary.completed += 1
        ctx.log(f"  {folder.name}: completed -> {store.OUTPUT}")
    elif result.status.is_terminal:
        store.write_state(folder, state)
        store.write_error(folder, result.error or result.status.value)
        ctx.summary.errors += 1
        ctx.log(f"  {folder.name}: {result.status.value}; wrote {store.ERROR}")
    else:
        store.write_state(folder, state)
        ctx.summary.pending += 1
        if ctx.verbose:
            ctx.log(f"  {folder.name}: {result.status.value}")
    return False  # waiting on the API, or just reached a terminal state


def _on_done(ctx: _Ctx, folder: Path) -> bool:
    _warn_on_drift(folder, ctx.log)
    return False


def _on_failed(ctx: _Ctx, folder: Path) -> bool:
    if ctx.verbose:
        ctx.log(f"  {folder.name}: failed (see {store.ERROR})")
    return False


_HANDLERS: dict[TaskState, Callable[[_Ctx, Path], bool]] = {
    TaskState.NEW: _on_new,
    TaskState.READY: _on_ready,
    TaskState.RUNNING: _on_running,
    TaskState.DONE: _on_done,
    TaskState.FAILED: _on_failed,
}


# --- helpers ----------------------------------------------------------------------


def _compose_output(answer: str | None, reasoning: str | None) -> str:
    text = (answer or "").rstrip("\n")
    if reasoning:
        text += "\n\n---\n\n## Reasoning summary\n\n" + reasoning.strip()
    return text


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


def _census_line(folders: list[Path]) -> str:
    """`new=… ready=… running=… done=… failed=…` over the given prompt folders."""
    counts = dict.fromkeys(TaskState, 0)
    for folder in folders:
        counts[store.classify(folder)] += 1
    return " ".join(f"{state.value}={counts[state]}" for state in TaskState)
