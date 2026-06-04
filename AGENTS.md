# AGENTS.md

This file provides guidance to Claude Code (claude.ai/code) and other coding agents when working with code in this repository.

## What this is

`gptproq` ("GPT Pro Queue") is a CLI that offloads large, curated reasoning prompts (plus attachments) to OpenAI's most capable models (default `gpt-5.5-pro`) and collects the answers later — a "submit now, retrieve later" job queue rather than an interactive chat. Python 3.12, managed with **uv**.

## Commands

- Install / sync deps: `uv sync`
- Run the CLI: `uv run gptproq <command>` (also `python -m gptproq`). Commands: `config` (`init`/`init-missing`/`get`/`set`/`path`), `prompt` (`new <name>` / `clone <src> <name>`), `sync`.
- Lint: `uv run ruff check .` — Format: `uv run ruff format .` (CI check: `uv run ruff format --check .`)
- **No test suite — by design.** Verify with `ruff`, `uv run gptproq --help`, and a live smoke test (needs a key in `~/.gptproq`; spends money). After touching `backend.py`, confirm the OpenAI call surface offline by instantiating `OpenAI(api_key="x")` and asserting `responses.create` accepts `background`/`store`/`reasoning` and that `batches.create`/`batches.retrieve`/`files.content` exist.

## Architecture (big picture)

**The filesystem is the database.** A queue directory holds **one folder per prompt**. Inside each folder, the all-caps *magic files* are tool-managed; **every other file is an attachment**: `PROMPT.md` · `CONFIG.json` (per-prompt model/effort/mode) · `STATE.json` (job state) · `OUTPUT.md` (answer) · `ERROR.txt` (failure). State is *derived* from which exist (`store.classify`); `sync` advances one step per run and is idempotent/cron-safe. Recompute by deleting `STATE.json`/`OUTPUT.md`/`ERROR.txt`.

Module roles (read together to understand a change):

- **`sync.py`** — the reconcile loop / state machine, the heart of the tool. Per folder, by magic files present: `PROMPT.md` only (new) → seed `CONFIG.json` from defaults **then submit in the same pass** (the new→ready transition is modeled separately but isn't a stopping point); `+CONFIG.json` (ready) → submit; `+STATE.json` (running) → poll; `+OUTPUT.md`/`+ERROR.txt` → done/failed, skip (with a drift warning if inputs changed). Owns per-prompt error isolation and the cron summary.
- **`backend.py`** — two backends behind one `submit`/`poll` interface, chosen by `mode`. **Both build the identical `/v1/responses` body** via `build_input()` (text/code attachments are concatenated into the prompt text; binaries are uploaded and become `input_file`/`input_image` parts). `BackgroundBackend` = `responses.create(background=True, store=True)` + `responses.retrieve`. `BatchBackend` = JSONL line → `files.create(purpose="batch")` → `batches.create(endpoint="/v1/responses", completion_window="24h")` → `batches.retrieve` → download/parse the output file by `custom_id`. Add backends/models here.
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
- openai SDK is **v2.x**. Background mode requires `store=True`. The Batch API targets `/v1/responses` and accepts `file_id` attachments in request bodies.
- A local `queue/` is git-ignored (user prompt data, not part of the repo).
