# AGENTS.md

This file provides guidance to Claude Code (claude.ai/code) and other coding agents when working with code in this repository.

## What this is

`gptproq` ("GPT Pro Queue") is a CLI that offloads large, curated reasoning prompts (plus attachments) to OpenAI's most capable models (default `gpt-5.5-pro`) and collects the answers later — a "submit now, retrieve later" job queue rather than an interactive chat. Python 3.12, managed with **uv**.

## Commands

- Install / sync deps: `uv sync`
- Run the CLI: `uv run gptproq <command>` (also `python -m gptproq`). Commands: `login` (hidden API-key prompt → `~/.gptproq`), `config` (`init`/`init-missing`/`get`/`set`/`path`/`cat`), `prompt` (`new <name>` / `clone <src> <name>`), `sync` (`-n N` loops every N seconds, like `watch`).
- Lint: `uv run ruff check .` — Format: `uv run ruff format .` (CI check: `uv run ruff format --check .`)
- **No test suite — by design.** Verify with `ruff`, `uv run gptproq --help`, and a live smoke test (needs a key in `~/.gptproq`; spends money). After touching `backend.py`, confirm the OpenAI call surface offline by instantiating `OpenAI(api_key="x")` and asserting `responses.create` accepts `background`/`store`/`reasoning` and that `batches.create`/`batches.retrieve`/`files.content` exist.

## Architecture (big picture)

**The filesystem is the database.** A queue directory holds **one folder per prompt**. Inside each folder, the all-caps *magic files* are tool-managed; **every other file is an attachment**: `PROMPT.md` · `CONFIG.json` (per-prompt model/effort/mode) · `STATE.json` (job state) · `OUTPUT.md` (answer) · `ERROR.txt` (failure). State is *derived* from which exist (`store.classify`); `sync` advances one step per run and is idempotent/cron-safe. Recompute by deleting `STATE.json`/`OUTPUT.md`/`ERROR.txt`.

Module roles (read together to understand a change):

- **`sync.py`** — the reconcile loop / state machine, the heart of the tool. `_drive` runs one prompt through transitions: `classify` the state from the magic files present → call that state's handler (`_on_new`/`_on_ready`/`_on_running`/`_on_done`/`_on_failed` via `_HANDLERS`) → the handler returns whether the prompt **advanced** to a state actionable now (loop again) or is now **waiting on the API / terminal** (stop until next `sync`). So a bare `PROMPT.md` advances new→ready→submitted in one run because only `_on_new` returns True. Owns per-prompt error isolation (transient → retry next run; terminal → `ERROR.txt`) and the cron summary.
- **`backend.py`** — two backends behind one `submit`/`poll` interface, chosen by `mode`. **Both build the identical `/v1/responses` body** via `build_input()` — text/code attachments are concatenated into the prompt text and binaries are sent **inline as base64** `input_file`/`input_image` parts, so **no Files are uploaded for attachments**. `BackgroundBackend` = `responses.create(background=True, store=True)` + `responses.retrieve` (creates no Files). `BatchBackend` uploads the request as a uniquely-named JSONL (`gptproq-<custom_id>-<uuid>.jsonl`, `purpose="batch"`; batch tagged `metadata={tool, prompt}`), polls, parses the output by `custom_id`, then **deletes the input/output/error files it owns** (by id, best-effort). Add backends/models here.
- **`store.py`** — filesystem layer: folder discovery, `classify`, magic-file paths, attachment gathering + content-type routing (`_route`: images→`input_image`; text/code→`input_text` (inlined by `build_input`); else→`input_file`), sha256, atomic writes, `CONFIG`/`STATE` read/write, `scaffold` (`prompt new`) and `clone` (`prompt clone` — copies inputs minus the generated files).
- **`config.py`** + **`models.py`** — `~/.gptproq` (TOML, `0600`) and the Pydantic models/enums (`Status`, `Kind`, `Mode`, `Effort`, `TaskConfig`=`CONFIG.json`, `StateFile`=`STATE.json`).

**Config layering:** built-in defaults → `~/.gptproq` (global) → per-prompt `CONFIG.json` (seeded by `gptproq new` or by the first `sync`, then the source of truth for that prompt). The API key comes **solely** from `~/.gptproq` — no env var.

**Strict global config:** `config.load_settings()` validates `~/.gptproq` against `_FileConfig` — every key must be present and well-typed, else it raises `ConfigError` (surfaced as a CLI error) rather than silently defaulting. `config init` writes all defaults; `config init-missing` backfills missing keys without touching existing values.

## Conventions & gotchas

- **Lean by design.** No test suite, no speculative abstractions; defaults to `gpt-5.5-pro` but does **not** restrict the model. Don't add tests/abstractions/guardrails unless asked. (The strict *config-file* validation above is an explicit, user-requested exception — it guards `~/.gptproq` integrity, not user inputs generally.)
- **`cli.py` must NOT use `from __future__ import annotations`** (Typer resolves annotations at runtime); every other module uses it. Typer params use `Annotated[...]` to avoid ruff `B008`.
- ruff config in `pyproject.toml` (line-length 100; `E,F,I,UP,B`). Enums are `StrEnum` (`UP042`).
- All magic-file writes go through `store.atomic_write`.
- **Transient API errors** (timeouts, 429, 5xx; `backend.is_transient`) are not recorded — left for the next `sync`. Only terminal failures write `ERROR.txt`.
- openai SDK is **v2.x**. Attachments are inlined as base64, never uploaded. Background mode requires `store=True` and creates no Files; the Batch API is the only path that creates Files (input JSONL + result files), which `BatchBackend.poll` deletes by owned id after reading results.
- `reasoning_summary` (config; default `auto`) sets how much reasoning the model returns; `_request_body` sends `reasoning={effort, summary}`, `_extract_reasoning` collects the summary text, and `_on_running` appends it to `OUTPUT.md`. It only adds the summary's own (small) output tokens — the real cost lever is `reasoning_effort`. Detailed summaries on the latest models may require org verification; `reasoning_summary none` disables.
- Defaults live in `models.py`: model `gpt-5.5-pro`, `reasoning_effort` `xhigh` (the max gpt-5.5-pro supports), `reasoning_summary` `auto`, `mode` `background`. Shell completion is Typer's built-in `--install-completion`/`--show-completion` (`add_completion=True`).
- A local `queue/` is git-ignored (user prompt data, not part of the repo).
