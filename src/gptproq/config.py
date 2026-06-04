"""Read and write the user config file at ``~/.gptproq`` (TOML)."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

import tomlkit
from pydantic import BaseModel, ValidationError

from .models import (
    DEFAULT_EFFORT,
    DEFAULT_MODE,
    DEFAULT_MODEL,
    DEFAULT_SUMMARY,
    Effort,
    Mode,
    ReasoningSummary,
    TaskConfig,
)

CONFIG_PATH = Path.home() / ".gptproq"

DEFAULTS: dict[str, str] = {
    "api_key": "",
    "model": DEFAULT_MODEL,
    "reasoning_effort": str(DEFAULT_EFFORT),
    "reasoning_summary": str(DEFAULT_SUMMARY),
    "mode": str(DEFAULT_MODE),
    "queue_dir": ".",
}
KNOWN_KEYS = tuple(DEFAULTS)


class ConfigError(Exception):
    """Raised when ``~/.gptproq`` is missing or invalid."""


class _FileConfig(BaseModel):
    """Strict schema for ``~/.gptproq`` — every key required, types checked."""

    api_key: str
    model: str
    reasoning_effort: Effort
    reasoning_summary: ReasoningSummary
    mode: Mode
    queue_dir: str


class Settings(BaseModel):
    """Effective global settings resolved from ``~/.gptproq``."""

    api_key: str | None
    model: str
    reasoning_effort: Effort
    reasoning_summary: ReasoningSummary
    mode: Mode
    queue_dir: Path

    def task_defaults(self) -> TaskConfig:
        """The per-prompt config a new prompt folder is seeded with."""
        return TaskConfig(
            model=self.model,
            reasoning_effort=self.reasoning_effort,
            reasoning_summary=self.reasoning_summary,
            mode=self.mode,
        )


def config_path() -> Path:
    return CONFIG_PATH


def _read_doc() -> tomlkit.TOMLDocument:
    if CONFIG_PATH.exists():
        return tomlkit.parse(CONFIG_PATH.read_text(encoding="utf-8"))
    return tomlkit.document()


def _write_doc(doc: tomlkit.TOMLDocument) -> None:
    CONFIG_PATH.write_text(tomlkit.dumps(doc), encoding="utf-8")
    os.chmod(CONFIG_PATH, 0o600)


def _fresh_doc() -> tomlkit.TOMLDocument:
    doc = tomlkit.document()
    doc.add(tomlkit.comment("gptproq configuration — edit via `gptproq config set <key> <value>`"))
    return doc


def init_config() -> bool:
    """Create ``~/.gptproq`` with every key at its default. Return ``False`` if it exists."""
    if CONFIG_PATH.exists():
        return False
    doc = _fresh_doc()
    for key, value in DEFAULTS.items():
        doc[key] = value
    _write_doc(doc)
    return True


def init_missing() -> list[str]:
    """Backfill any missing default keys in ``~/.gptproq`` (keeping existing values)."""
    existed = CONFIG_PATH.exists()
    doc = _read_doc() if existed else _fresh_doc()
    added = [k for k in DEFAULTS if k not in doc]
    for key in added:
        doc[key] = DEFAULTS[key]
    if added or not existed:
        _write_doc(doc)
    return added


def get_value(key: str) -> str | None:
    value = _read_doc().get(key)
    return None if value is None else str(value)


def set_value(key: str, value: str) -> None:
    doc = _read_doc()
    doc[key] = value
    _write_doc(doc)


def load_settings() -> Settings:
    """Strictly load ``~/.gptproq``: every expected key must be present and well-typed."""
    if not CONFIG_PATH.exists():
        raise ConfigError(f"No config file at {CONFIG_PATH}. Run `gptproq config init`.")
    try:
        data = tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Malformed TOML in {CONFIG_PATH}: {exc}") from exc
    try:
        fc = _FileConfig.model_validate(data)
    except ValidationError as exc:
        problems = "; ".join(f"{e['loc'][0]}: {e['msg']}" for e in exc.errors())
        raise ConfigError(
            f"Invalid config at {CONFIG_PATH}: {problems}. "
            "Run `gptproq config init-missing` to add missing keys, then fix any invalid values."
        ) from exc
    return Settings(
        api_key=fc.api_key or None,
        model=fc.model,
        reasoning_effort=fc.reasoning_effort,
        reasoning_summary=fc.reasoning_summary,
        mode=fc.mode,
        queue_dir=Path(fc.queue_dir).expanduser(),
    )
