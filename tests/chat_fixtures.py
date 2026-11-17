"""Helpers to build chats in a fake Cursor global DB and matching snapshots."""

import json
import sqlite3
from pathlib import Path

from cursor_saves import export, paths


def composer_data(cid: str, bubble_ids: list[str], name: str = "Chat") -> dict:
    return {
        "composerId": cid,
        "name": name,
        "createdAt": 1700000000000,
        "lastUpdatedAt": 1700000000000,
        "unifiedMode": "agent",
        "fullConversationHeadersOnly": [{"bubbleId": b, "type": 1} for b in bubble_ids],
    }


def add_chat(cid: str, bubble_ids: list[str], name: str = "Chat") -> None:
    """Write a chat (composerData + bubbles) directly into the global DB."""
    conn = sqlite3.connect(str(paths.get_global_db_path()))
    conn.execute(
        "INSERT OR REPLACE INTO cursorDiskKV VALUES (?, ?)",
        (f"composerData:{cid}", json.dumps(composer_data(cid, bubble_ids, name))),
    )
    for b in bubble_ids:
        conn.execute(
            "INSERT OR REPLACE INTO cursorDiskKV VALUES (?, ?)",
            (f"bubbleId:{cid}:{b}", json.dumps({"bubbleId": b, "text": f"msg {b}"})),
        )
    conn.commit()
    conn.close()


def register_in_workspace(ws_dir: Path, cids: list[str]) -> None:
    """Register chats in a workspace DB using the Cursor 2.x allComposers list."""
    conn = sqlite3.connect(str(ws_dir / "state.vscdb"))
    data = {"allComposers": [{"composerId": c, "name": c} for c in cids], "selectedComposerIds": []}
    conn.execute(
        "INSERT OR REPLACE INTO ItemTable VALUES ('composer.composerData', ?)", (json.dumps(data),)
    )
    conn.commit()
    conn.close()


def write_snapshot(
    cid: str,
    bubble_ids: list[str],
    project_path: str,
    name: str = "Chat",
    source: str = "machine-b",
    exported_at: str = "2026-01-01T00:00:00+00:00",
) -> Path:
    """Save a snapshot as another machine would have pushed it."""
    snapshot = {
        "version": 3,
        "exportedAt": exported_at,
        "sourceMachine": source,
        "sourceHost": None,
        "sourceProjectPath": project_path,
        "projectIdentifier": paths.get_project_identifier(project_path),
        "composerId": cid,
        "composerData": composer_data(cid, bubble_ids, name),
        "contentBlobs": {},
        "bubbleEntries": {b: {"bubbleId": b, "text": f"msg {b}"} for b in bubble_ids},
        "checkpoints": {},
        "agentBlobs": {},
        "transcript": None,
        "messageContexts": {},
    }
    return export.save_snapshot(snapshot, paths.get_snapshots_dir())


def local_chat(cid: str):
    conn = sqlite3.connect(str(paths.get_global_db_path()))
    row = conn.execute("SELECT value FROM cursorDiskKV WHERE key=?", (f"composerData:{cid}",)).fetchone()
    conn.close()
    return json.loads(row[0]) if row else None


def all_chat_ids() -> set[str]:
    conn = sqlite3.connect(str(paths.get_global_db_path()))
    rows = conn.execute("SELECT key FROM cursorDiskKV WHERE key LIKE 'composerData:%'").fetchall()
    conn.close()
    return {r[0].split(":", 1)[1] for r in rows}
