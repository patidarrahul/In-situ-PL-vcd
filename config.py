"""Small JSON-backed settings store (remembers the chosen data folder)."""

from __future__ import annotations

import json
from pathlib import Path

DEFAULT_CONFIG_PATH = Path.home() / ".pumpdown_pl_monitor.json"
PROJECT_ROOT = Path(__file__).resolve().parent


def default_data_dir() -> Path:
    """Recordings live in data/ next to the app, not in the project root."""
    path = PROJECT_ROOT / "data"
    path.mkdir(exist_ok=True)
    return path


def load_settings(path: str | Path | None = None) -> dict:
    config_path = Path(path or DEFAULT_CONFIG_PATH)
    try:
        data = json.loads(config_path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_settings(settings: dict, path: str | Path | None = None) -> None:
    config_path = Path(path or DEFAULT_CONFIG_PATH)
    try:
        config_path.write_text(json.dumps(settings, indent=2))
    except OSError:
        pass  # a read-only home directory should not break logging


def resolve_data_dir(value: str | Path | None) -> Path:
    """Usable directory for CSV output, falling back to the project data/ folder."""
    if value:
        candidate = Path(value).expanduser()
        if candidate.is_dir():
            return candidate
    return default_data_dir()


def resolve_browse_dir(
    value: str | Path | None = None,
    fallback: str | Path | None = None,
) -> Path:
    """Directory for file dialogs; accepts a file path and uses its parent."""
    if value:
        candidate = Path(value).expanduser()
        if candidate.is_file():
            candidate = candidate.parent
        if candidate.is_dir():
            return candidate
    return resolve_data_dir(fallback)


def remember_browse_dir(settings: dict, path: str | Path | None) -> Path:
    """Store the folder of a chosen file/folder and return it for the next dialog."""
    directory = resolve_browse_dir(
        path, fallback=settings.get("last_browse_dir") or settings.get("data_dir")
    )
    if path:
        chosen = Path(path).expanduser()
        folder = chosen if chosen.is_dir() else chosen.parent
        if folder.is_dir():
            settings["last_browse_dir"] = str(folder)
            save_settings(settings)
            return folder
    return directory
