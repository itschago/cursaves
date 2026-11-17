"""Helpers to build chats in a fake Cursor global DB and matching snapshots."""

import json
import sqlite3
from pathlib import Path

from cursor_saves import export, paths


CHUNK_SIZE = 2  # Cursor uses 256; small here so tests span several chunks


def headers(bubble_ids: list[str]) -> list[dict]:
    return [{"bubbleId": b, "type": 1} for b in bubble_ids]


def header_chunks(bubble_ids: list[str]) -> dict[str, list]:
    hs = headers(bubble_ids)
    return {
        f"{i // CHUNK_SIZE:06d}": hs[i:i + CHUNK_SIZE] for i in range(0, len(hs), CHUNK_SIZE)
    }


def composer_data(
    cid: str, bubble_ids: list[str], name: str = "Chat", chunked: bool = False,
) -> dict:
    """composerData; *chunked* mimics _v 18+ (headers in composerConvHeaders chunks)."""
    data = {
        "composerId": cid,
        "name": name,
        "createdAt": 1700000000000,
        "lastUpdatedAt": 1700000000000,
        "unifiedMode": "agent",
        "fullConversationHeadersOnly": headers(bubble_ids),
    }
    if chunked:
        data["_v"] = 18
        data["fullConversationHeadersOnly"] = []
        data["headerLog"] = {
            "chunkSize": CHUNK_SIZE,
            "chunkCount": len(header_chunks(bubble_ids)),
            "count": len(bubble_ids),
            "generation": "gen-" + "-".join(bubble_ids),
        }
    return data


def add_chat(cid: str, bubble_ids: list[str], name: str = "Chat", chunked: bool = False) -> None:
    """Write a chat (composerData + bubbles) directly into the global DB."""
    conn = sqlite3.connect(str(paths.get_global_db_path()))
    conn.execute(
        "INSERT OR REPLACE INTO cursorDiskKV VALUES (?, ?)",
        (f"composerData:{cid}", json.dumps(composer_data(cid, bubble_ids, name, chunked))),
    )
    for b in bubble_ids:
        conn.execute(
            "INSERT OR REPLACE INTO cursorDiskKV VALUES (?, ?)",
            (f"bubbleId:{cid}:{b}", json.dumps({"bubbleId": b, "text": f"msg {b}"})),
        )
    if chunked:
        conn.execute("DELETE FROM cursorDiskKV WHERE key LIKE ?", (f"composerConvHeaders:{cid}:%",))
        for suffix, chunk in header_chunks(bubble_ids).items():
            conn.execute(
                "INSERT OR REPLACE INTO cursorDiskKV VALUES (?, ?)",
                (f"composerConvHeaders:{cid}:{suffix}", json.dumps(chunk)),
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
    chunked: bool = False,
    include_chunks: bool = True,
) -> Path:
    """Save a snapshot as another machine would have pushed it.

    *include_chunks=False* reproduces snapshots from older cursaves, which
    didn't export the header chunks of chunked chats.
    """
    snapshot = {
        "version": 3,
        "exportedAt": exported_at,
        "sourceMachine": source,
        "sourceHost": None,
        "sourceProjectPath": project_path,
        "projectIdentifier": paths.get_project_identifier(project_path),
        "composerId": cid,
        "composerData": composer_data(cid, bubble_ids, name, chunked),
        "contentBlobs": {},
        "bubbleEntries": {b: {"bubbleId": b, "text": f"msg {b}"} for b in bubble_ids},
        "checkpoints": {},
        "agentBlobs": {},
        "transcript": None,
        "messageContexts": {},
    }
    if chunked and include_chunks:
        snapshot["headerChunks"] = header_chunks(bubble_ids)
    return export.save_snapshot(snapshot, paths.get_snapshots_dir())


def local_header_ids(cid: str) -> list[str]:
    from cursor_saves import db

    with db.CursorDB(paths.get_global_db_path()) as cdb:
        return [h["bubbleId"] for h in cdb.get_conversation_headers(cid)]


def local_header_chunk_keys(cid: str) -> list[str]:
    conn = sqlite3.connect(str(paths.get_global_db_path()))
    rows = conn.execute(
        "SELECT key FROM cursorDiskKV WHERE key LIKE ? ORDER BY key", (f"composerConvHeaders:{cid}:%",)
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


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
