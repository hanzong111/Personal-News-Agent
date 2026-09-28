"""Personal News Agent pipeline. The version lives in the repo-root VERSION file (Semantic Versioning)."""
from pathlib import Path

try:
    __version__ = (Path(__file__).resolve().parent.parent / "VERSION").read_text().strip()
except OSError:
    __version__ = "unknown"
