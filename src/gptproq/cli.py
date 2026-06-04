"""Typer CLI entry point for gptproq."""

from pathlib import Path
from typing import Annotated

import typer

from . import config, store
from .config import ConfigError, Settings
from .models import Effort, Mode
from .sync import run_sync

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="GPT Pro Queue — offload big reasoning prompts to OpenAI and collect answers later.",
)
config_app = typer.Typer(no_args_is_help=True, help="Manage the ~/.gptproq config file.")
prompt_app = typer.Typer(no_args_is_help=True, help="Create and copy prompt folders.")
app.add_typer(config_app, name="config")
app.add_typer(prompt_app, name="prompt")


def _mask(secret: str) -> str:
    if not secret:
        return "(empty)"
    if len(secret) <= 8:
        return "****"
    return f"{secret[:4]}…{secret[-4:]}"


def _load_settings() -> Settings:
    try:
        return config.load_settings()
    except ConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


@config_app.command("init")
def config_init() -> None:
    """Create ~/.gptproq with every key at its default (no-op if it exists)."""
    if config.init_config():
        typer.echo(f"Created {config.config_path()}")
    else:
        typer.echo(f"{config.config_path()} already exists; leaving it untouched.")


@config_app.command("init-missing")
def config_init_missing() -> None:
    """Backfill any missing keys in ~/.gptproq with default values."""
    added = config.init_missing()
    if added:
        typer.echo(f"Added missing keys: {', '.join(added)}")
    else:
        typer.echo("Config already has all keys; nothing to add.")


@config_app.command("path")
def config_path_cmd() -> None:
    """Print the config file path."""
    typer.echo(str(config.config_path()))


@config_app.command("get")
def config_get(key: str) -> None:
    """Print a config value (api_key is masked)."""
    value = config.get_value(key)
    if value is None:
        typer.echo(f"{key} is not set", err=True)
        raise typer.Exit(1)
    typer.echo(_mask(value) if key == "api_key" else value)


@config_app.command("set")
def config_set(key: str, value: str) -> None:
    """Set a config value, e.g. `config set api_key sk-...` or `config set mode batch`."""
    if key not in config.KNOWN_KEYS:
        typer.echo(f"Unknown key '{key}'. Known: {', '.join(config.KNOWN_KEYS)}", err=True)
        raise typer.Exit(1)
    if key == "reasoning_effort" and value not in {e.value for e in Effort}:
        choices = ", ".join(e.value for e in Effort)
        typer.echo(f"Invalid reasoning_effort '{value}'. Choose from: {choices}", err=True)
        raise typer.Exit(1)
    if key == "mode" and value not in {m.value for m in Mode}:
        choices = ", ".join(m.value for m in Mode)
        typer.echo(f"Invalid mode '{value}'. Choose from: {choices}", err=True)
        raise typer.Exit(1)
    config.set_value(key, value)
    typer.echo(f"Set {key} = {_mask(value) if key == 'api_key' else value}")


@prompt_app.command("new")
def prompt_new(
    name: Annotated[str, typer.Argument(help="Name of the new prompt folder.")],
    directory: Annotated[
        Path | None, typer.Option("--dir", "-d", help="Queue directory (default from config).")
    ] = None,
) -> None:
    """Create a prompt folder with a PROMPT.md template and a seeded CONFIG.json."""
    settings = _load_settings()
    folder = (directory or settings.queue_dir) / name
    if folder.exists():
        typer.echo(f"Already exists: {folder}", err=True)
        raise typer.Exit(1)
    store.scaffold(folder, settings.task_defaults())
    typer.echo(f"Created {folder}/")
    typer.echo(f"Edit {store.PROMPT}, add attachments, then run `gptproq sync`.")


@prompt_app.command("clone")
def prompt_clone(
    source: Annotated[str, typer.Argument(help="Existing prompt folder to copy from.")],
    name: Annotated[str, typer.Argument(help="Name of the new prompt folder.")],
    directory: Annotated[
        Path | None, typer.Option("--dir", "-d", help="Queue directory (default from config).")
    ] = None,
) -> None:
    """Copy a prompt's inputs (PROMPT.md, CONFIG.json, attachments) to a new folder,
    dropping the generated STATE.json / OUTPUT.md / ERROR.txt."""
    settings = _load_settings()
    queue = directory or settings.queue_dir
    src, dst = queue / source, queue / name
    if not store.prompt_path(src).is_file():
        typer.echo(f"Not a prompt folder: {src}", err=True)
        raise typer.Exit(1)
    if dst.exists():
        typer.echo(f"Already exists: {dst}", err=True)
        raise typer.Exit(1)
    store.clone(src, dst)
    typer.echo(f"Cloned {source} -> {name} (inputs only)")


@app.command()
def sync(
    directory: Annotated[
        Path | None, typer.Option("--dir", "-d", help="Queue directory (default from config).")
    ] = None,
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="Show per-prompt status.")
    ] = False,
) -> None:
    """Submit new prompts, poll in-flight jobs, and write completed answers."""
    settings = _load_settings()
    queue = directory or settings.queue_dir
    if not queue.is_dir():
        typer.echo(f"Queue directory not found: {queue}", err=True)
        raise typer.Exit(1)
    run_sync(settings, queue, verbose=verbose)
