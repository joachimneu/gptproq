"""gptproq — GPT Pro Queue."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("gptproq")
except PackageNotFoundError:  # pragma: no cover - running from a non-installed tree
    __version__ = "0.0.0"
