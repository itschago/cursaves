"""Local, per-machine sync state (~/.config/cursaves/sync_state.json).

Kept outside ~/.cursaves/ so the git backend's `git clean` can't remove it.
"""

import json
from pathlib import Path


def get_path() -> Path:
    return Path.home() / ".config" / "cursaves" / "sync_state.json"


def load() -> dict:
    path = get_path()
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save(state: dict) -> None:
    path = get_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2))


def diverged_copy_ids() -> set[str]:
    """IDs of local chats created by importing a diverged remote version.

    These mirror another machine's work, so sync must not push them back.
    """
    return set(load().get("diverged_copies", []))


def record_diverged_copy(copy_id: str) -> None:
    state = load()
    copies = state.setdefault("diverged_copies", [])
    if copy_id not in copies:
        copies.append(copy_id)
        save(state)
