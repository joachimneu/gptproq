"""Filesystem layer: each prompt is a folder; the set of folders is the database."""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from enum import StrEnum
from pathlib import Path

from .models import Attachment, Kind, StateFile, TaskConfig

PROMPT = "PROMPT.md"
CONFIG = "CONFIG.json"
STATE = "STATE.json"
OUTPUT = "OUTPUT.md"
ERROR = "ERROR.txt"
MAGIC = frozenset({PROMPT, CONFIG, STATE, OUTPUT, ERROR})
GENERATED = frozenset({STATE, OUTPUT, ERROR})  # written by sync; not copied on clone

IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif"})
# fmt: off
TEXT_EXTS = frozenset({
    ".md", ".txt", ".rst", ".tex", ".py", ".js", ".ts", ".tsx", ".jsx", ".rs",
    ".go", ".java", ".c", ".h", ".cpp", ".cc", ".hpp", ".rb", ".sh", ".bash",
    ".zsh", ".json", ".jsonl", ".toml", ".yaml", ".yml", ".html", ".css",
    ".sql", ".r", ".kt", ".swift", ".php", ".lua", ".scala", ".ini", ".cfg",
    ".bib", ".aux", ".cls", ".sty", ".bbl",  # LaTeX / BibTeX (.tex already listed)
})
# fmt: on

PROMPT_TEMPLATE = """\
Write your prompt here.

Drop any supporting files (PDFs, images, data, notes) into this folder and they
are attached automatically on the next `gptproq sync`.
"""


class TaskState(StrEnum):
    NEW = "new"  # PROMPT.md only -> write CONFIG.json
    READY = "ready"  # + CONFIG.json -> submit
    RUNNING = "running"  # + STATE.json -> poll
    DONE = "done"  # + OUTPUT.md
    FAILED = "failed"  # + ERROR.txt


def prompt_dirs(queue: Path) -> list[Path]:
    """Immediate subdirectories of ``queue`` that contain a ``PROMPT.md``."""
    return sorted(d for d in queue.iterdir() if d.is_dir() and (d / PROMPT).is_file())


def prompt_path(folder: Path) -> Path:
    return folder / PROMPT


def config_path(folder: Path) -> Path:
    return folder / CONFIG


def state_path(folder: Path) -> Path:
    return folder / STATE


def output_path(folder: Path) -> Path:
    return folder / OUTPUT


def error_path(folder: Path) -> Path:
    return folder / ERROR


def classify(folder: Path) -> TaskState:
    """Determine a prompt folder's state from which magic files exist."""
    if output_path(folder).exists():
        return TaskState.DONE
    if error_path(folder).exists():
        return TaskState.FAILED
    if state_path(folder).exists():
        return TaskState.RUNNING
    if config_path(folder).exists():
        return TaskState.READY
    return TaskState.NEW


def read_prompt(folder: Path) -> str:
    return prompt_path(folder).read_text(encoding="utf-8")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def atomic_write(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically (temp file in the same dir + ``os.replace``)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".gptproq-tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def _route(path: Path) -> Kind:
    ext = path.suffix.lower()
    if ext in IMAGE_EXTS:
        return Kind.INPUT_IMAGE
    if ext in TEXT_EXTS:
        return Kind.INPUT_TEXT
    return Kind.INPUT_FILE


def gather_attachments(folder: Path) -> list[Attachment]:
    """Every non-magic, non-hidden file directly in ``folder``, routed by type."""
    items: list[Attachment] = []
    for p in sorted(folder.iterdir()):
        if not p.is_file() or p.name in MAGIC or p.name.startswith("."):
            continue
        items.append(Attachment(path=p.name, sha256=sha256_file(p), kind=_route(p)))
    return items


def read_config(folder: Path) -> TaskConfig:
    return TaskConfig.model_validate_json(config_path(folder).read_text(encoding="utf-8"))


def write_config(folder: Path, cfg: TaskConfig) -> None:
    atomic_write(config_path(folder), cfg.model_dump_json(indent=2) + "\n")


def read_state(folder: Path) -> StateFile:
    return StateFile.model_validate_json(state_path(folder).read_text(encoding="utf-8"))


def write_state(folder: Path, state: StateFile) -> None:
    atomic_write(state_path(folder), state.model_dump_json(indent=2) + "\n")


def write_output(folder: Path, text: str) -> None:
    atomic_write(output_path(folder), text if text.endswith("\n") else text + "\n")


def write_error(folder: Path, message: str) -> None:
    atomic_write(error_path(folder), message.rstrip("\n") + "\n")


def scaffold(folder: Path, cfg: TaskConfig) -> None:
    """Create a new prompt folder with a ``PROMPT.md`` template and seeded ``CONFIG.json``."""
    folder.mkdir(parents=True, exist_ok=False)
    atomic_write(prompt_path(folder), PROMPT_TEMPLATE)
    write_config(folder, cfg)


def clone(src: Path, dst: Path) -> None:
    """Copy a prompt folder's inputs (``PROMPT.md``, ``CONFIG.json``, attachments) to
    ``dst``, omitting the generated files (``STATE.json`` / ``OUTPUT.md`` / ``ERROR.txt``)."""
    dst.mkdir(parents=True, exist_ok=False)
    for item in sorted(src.iterdir()):
        if item.is_file() and item.name not in GENERATED:
            shutil.copy2(item, dst / item.name)
