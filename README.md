# gptproq

**GPT Pro Queue** — a filesystem-as-database job queue for offloading large,
curated reasoning prompts (with attachments) to OpenAI's most capable models
(default `gpt-5.5-pro`) and collecting the answers later.

You write each prompt as a **folder** of files, then run `gptproq sync` whenever
you like (by hand or from cron). It submits new prompts, polls running ones, and
writes answers back into each folder.

## How it works

The queue directory holds **one folder per prompt**. Inside each folder, the
all-caps *magic files* are managed by the tool; **every other file is sent to the
model as an attachment**:

```
my-analysis/
  PROMPT.md      # your prompt            (magic)
  CONFIG.json    # model / effort / mode  (magic)
  STATE.json     # job state              (magic, written by sync)
  OUTPUT.md      # the answer             (magic, written by sync)
  ERROR.txt      # only on failure        (magic, written by sync)
  paper.pdf      # ← attachment
  figure.png     # ← attachment
  notes.md       # ← attachment
```

A folder advances one step per `sync`, based on which magic files exist:

| Magic files present | State | `sync` does |
|---|---|---|
| `PROMPT.md` | new | seed `CONFIG.json`, then submit → `STATE.json` |
| + `CONFIG.json` | ready | submit → `STATE.json` |
| + `STATE.json` | running | poll; on success write `OUTPUT.md` |
| + `OUTPUT.md` | done | skip |
| + `ERROR.txt` | failed | skip |

A bare `PROMPT.md` is configured (from your global defaults) **and** submitted in
the same `sync` — `new` → `ready` are distinct transitions, not a stopping point.
Use `gptproq prompt new` to pre-create a folder with `CONFIG.json` so you can tune
it before the first `sync`.

`sync` is idempotent and cron-safe. **To recompute a prompt**, delete its
`STATE.json` / `OUTPUT.md` / `ERROR.txt` and run `sync` again.

### Attachments

Every non-magic file in a folder is sent, routed by type:

- **text / code** (`.md`, `.py`, `.tex`, …) → inlined into the prompt text under
  a `===== file: name =====` header.
- **PDFs** → sent inline as a file input (the model gets text + page images).
- **spreadsheets** (`.csv`, `.xlsx`, …) → sent inline as a file input.
- **images** (`.png`, `.jpg`, …) → sent inline as an image input.
- anything else → sent inline as a file input.

Everything is sent **inline (base64)**, so background runs leave **no files in
your OpenAI storage**. Batch mode must upload its request as a file (named
`gptproq-<prompt>-<uuid>.jsonl`) and OpenAI returns result files; gptproq deletes
all of them automatically once it has read the answer.

### Backends

Two submission modes, set by `mode`:

- `background` (default) — Responses background mode; answers in minutes.
- `batch` — the Batch API; **50% cheaper**, up to 24h turnaround. Best for
  fire-and-forget runs.

## Install

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run gptproq --help
```

## Configure

All settings live in `~/.gptproq` (TOML, created `0600`). `config init` writes the
file with every key at its default; each prompt's `CONFIG.json` is seeded from
these and may override `model` / `reasoning_effort` / `mode`.

```bash
uv run gptproq config init                       # write ~/.gptproq with all defaults
uv run gptproq config set api_key sk-...
uv run gptproq config set mode batch             # background | batch
uv run gptproq config set reasoning_effort high  # low | medium | high | xhigh
uv run gptproq config set reasoning_summary auto # auto | concise | detailed | none
uv run gptproq config set model gpt-5.5-pro
uv run gptproq config get queue_dir
uv run gptproq config path
uv run gptproq config cat                        # print the whole config file
```

The config is **strictly validated** when read: every expected key must be
present and well-typed, otherwise `sync`/`new` error instead of silently using a
default. If you upgrade and a key is missing, backfill it:

```bash
uv run gptproq config init-missing               # add missing keys, keep existing values
```

The API key is read **only** from `~/.gptproq` (`config set api_key …`).

## Use

```bash
uv run gptproq prompt new my-analysis -d ~/queue   # create ~/queue/my-analysis/
# edit ~/queue/my-analysis/PROMPT.md, drop attachments into the folder
uv run gptproq sync -d ~/queue                     # submit new prompts; poll running ones
uv run gptproq sync -d ~/queue -v                  # ...with per-prompt status

# reuse a prompt: copy its inputs (prompt + attachments + config), drop results
uv run gptproq prompt clone my-analysis my-analysis-v2 -d ~/queue
```

The queue directory comes from `-d`, else the `queue_dir` setting (default `.`),
so you can also just `cd` into your queue and run `gptproq sync`.

`sync` prints a one-line summary and exits, so it drops into cron:

```cron
*/10 * * * * cd ~/queue && uv run gptproq sync >> .sync.log 2>&1
```

## Notes

- Built for top-tier Pro reasoning; defaults to `gpt-5.5-pro` (you can set any
  model, but that's the intent).
- Background responses have a limited retrieval window — run `sync` regularly; an
  unretrievable response is recorded as `expired` in `ERROR.txt`.
- Transient API errors (timeouts, rate limits, 5xx) aren't recorded as failures;
  they retry on the next run.
- `reasoning_summary` (default `auto`) controls how much of the model's thinking
  is returned and appended to `OUTPUT.md`. It only adds the summary's own small
  token count — the real cost lever is `reasoning_effort`. Detailed summaries on
  the latest models may require organization verification; use `none` to disable.
