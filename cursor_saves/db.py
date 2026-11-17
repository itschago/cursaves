"""Safe SQLite reader/writer for Cursor's state.vscdb databases."""

import json
import shutil
import sqlite3
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional

from . import paths


class CursorRunningError(RuntimeError):
    """Raised when a write to Cursor's databases is attempted while Cursor is open."""


_allow_writes_while_running = False


def allow_writes_while_cursor_running(allow: bool = True) -> None:
    """Disable (or re-enable) the Cursor-running check for writes (--force)."""
    global _allow_writes_while_running
    _allow_writes_while_running = allow


def ensure_cursor_closed() -> None:
    """Raise CursorRunningError if Cursor is running and writes aren't forced.

    Cursor keeps chat state in memory and rewrites its databases on exit,
    so anything written while it is open can be silently overwritten.
    """
    if not _allow_writes_while_running and paths.is_cursor_running():
        raise CursorRunningError(
            "Cursor is running. Quit Cursor completely (Cmd+Q / File > Exit), "
            "then re-run this command. Use --force to write anyway (not recommended)."
        )


class CursorDB:
    """Safe interface to a Cursor state.vscdb database.

    All reads are performed on a temporary copy of the database to avoid
    locking conflicts with a running Cursor instance. Writes operate on
    the original file and require Cursor to be closed.
    """

    def __init__(self, db_path: Path, direct: bool = False):
        """*direct* reads the original file instead of a temp copy.

        Use it only when Cursor is closed (e.g. during import), where
        copying a multi-GB database for every lookup is pure overhead.
        """
        self.db_path = db_path
        self.direct = direct
        self._tmp_path: Optional[Path] = None
        self._conn: Optional[sqlite3.Connection] = None
        self._write_conn: Optional[sqlite3.Connection] = None
        self._in_transaction = False

    def _ensure_read_copy(self) -> sqlite3.Connection:
        """Copy the database to a temp file and open a read-only connection."""
        if self._conn is not None:
            return self._conn

        if not self.db_path.exists():
            raise FileNotFoundError(f"Database not found: {self.db_path}")

        if self.direct:
            self._conn = sqlite3.connect(f"{self.db_path.as_uri()}?mode=ro", uri=True)
            return self._conn

        tmp_dir = tempfile.mkdtemp(prefix="cursaves-")
        tmp_db = Path(tmp_dir) / "state.vscdb"
        try:
            snapshot_sqlite_db(self.db_path, tmp_db)
        except BaseException:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise

        self._tmp_path = tmp_db
        self._conn = sqlite3.connect(str(tmp_db))
        return self._conn

    def close(self):
        """Close connections and clean up temp files."""
        if self._conn:
            self._conn.close()
            self._conn = None
        if self._write_conn:
            self._write_conn.close()
            self._write_conn = None
        if self._tmp_path:
            shutil.rmtree(self._tmp_path.parent, ignore_errors=True)
            self._tmp_path = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    # ── Read operations (on temp copy) ──────────────────────────────

    def get_item(self, key: str, table: str = "ItemTable") -> Optional[str]:
        """Get a value from the key-value store."""
        conn = self._ensure_read_copy()
        try:
            row = conn.execute(
                f"SELECT value FROM {table} WHERE key = ?", (key,)
            ).fetchone()
            if row is None:
                return None
            val = row[0]
            if isinstance(val, bytes):
                return val.decode("utf-8", errors="replace")
            return val
        except sqlite3.OperationalError:
            return None

    def get_item_binary(self, key: str, table: str = "ItemTable") -> Optional[bytes]:
        """Get a raw binary value from the key-value store."""
        conn = self._ensure_read_copy()
        try:
            row = conn.execute(
                f"SELECT value FROM {table} WHERE key = ?", (key,)
            ).fetchone()
            if row is None:
                return None
            val = row[0]
            if isinstance(val, str):
                return val.encode("utf-8")
            return val
        except sqlite3.OperationalError:
            return None

    def get_disk_kv(self, key: str) -> Optional[str]:
        """Get a value from the cursorDiskKV table."""
        return self.get_item(key, table="cursorDiskKV")

    def list_keys(self, prefix: str = "", table: str = "cursorDiskKV") -> list[str]:
        """List all keys in a table, optionally filtered by prefix."""
        conn = self._ensure_read_copy()
        try:
            if prefix:
                # A range on the unique key index; LIKE would scan the table.
                upper = prefix[:-1] + chr(ord(prefix[-1]) + 1)
                rows = conn.execute(
                    f"SELECT key FROM {table} WHERE key >= ? AND key < ?", (prefix, upper)
                ).fetchall()
            else:
                rows = conn.execute(f"SELECT key FROM {table}").fetchall()
            return [r[0] for r in rows]
        except sqlite3.OperationalError:
            return []

    def count_keys_by_chat_prefix(
        self, key_type: str, table: str = "cursorDiskKV"
    ) -> dict[str, int]:
        """Count keys grouped by chat ID for a given key type prefix.

        For example, key_type="bubbleId" counts all keys like
        "bubbleId:<uuid>:..." and returns {<uuid>: count, ...}.

        Uses a single SQL query — efficient even on large databases.
        """
        conn = self._ensure_read_copy()
        result: dict[str, int] = {}
        try:
            prefix = key_type + ":"
            rows = conn.execute(
                f"""SELECT SUBSTR(key, {len(prefix) + 1}, 36) AS cid, COUNT(*)
                    FROM {table}
                    WHERE key LIKE ?
                    GROUP BY cid""",
                (prefix + "%",),
            ).fetchall()
            for cid, count in rows:
                if cid and len(cid) == 36:
                    result[cid] = count
        except sqlite3.OperationalError:
            pass
        return result

    def get_json(self, key: str, table: str = "cursorDiskKV") -> Optional[Any]:
        """Get and parse a JSON value."""
        raw = self.get_item(key, table=table)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    # ── Write operations (on original file) ─────────────────────────

    def _get_write_conn(self) -> sqlite3.Connection:
        """Get or create a connection for write operations on the ORIGINAL database."""
        if self._write_conn is None:
            ensure_cursor_closed()
            self._write_conn = sqlite3.connect(str(self.db_path))
        return self._write_conn

    @contextmanager
    def transaction(self):
        """Group writes into one transaction: all of them land, or none do."""
        conn = self._get_write_conn()
        conn.execute("BEGIN IMMEDIATE")
        self._in_transaction = True
        try:
            yield self
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            self._in_transaction = False

    def _begin(self, conn: sqlite3.Connection) -> None:
        if not self._in_transaction:
            conn.execute("BEGIN")

    def _commit(self, conn: sqlite3.Connection) -> None:
        if not self._in_transaction:
            conn.execute("COMMIT")

    def _rollback(self, conn: sqlite3.Connection) -> None:
        if not self._in_transaction:
            conn.execute("ROLLBACK")

    def write_item(self, key: str, value: str, table: str = "ItemTable"):
        """Write a value to the key-value store on the ORIGINAL database.

        This operates directly on the original file, not the temp copy.
        Caller must ensure Cursor is not running.
        """
        self.write_batch([(key, value)], table=table)

    def write_disk_kv(self, key: str, value: str):
        """Write a value to cursorDiskKV on the ORIGINAL database."""
        self.write_item(key, value, table="cursorDiskKV")

    def write_json(self, key: str, data: Any, table: str = "cursorDiskKV"):
        """Write a JSON value to the ORIGINAL database."""
        self.write_item(key, json.dumps(data, separators=(",", ":")), table=table)

    def write_batch(self, items: list[tuple[str, str]], table: str = "cursorDiskKV"):
        """Write multiple key-value pairs in a single transaction.

        Much faster than calling write_item() in a loop -- uses one
        connection and one transaction for all items.
        """
        conn = self._get_write_conn()
        self._begin(conn)
        try:
            conn.executemany(
                f"INSERT OR REPLACE INTO {table} (key, value) VALUES (?, ?)",
                items,
            )
            self._commit(conn)
        except Exception:
            self._rollback(conn)
            raise

    def write_json_batch(self, items: list[tuple[str, Any]], table: str = "cursorDiskKV"):
        """Write multiple JSON key-value pairs in a single transaction."""
        serialized = [
            (key, json.dumps(data, separators=(",", ":")))
            for key, data in items
        ]
        self.write_batch(serialized, table=table)

    def delete_keys(self, keys: list[str], table: str = "cursorDiskKV") -> int:
        """Delete multiple keys in a single transaction on the ORIGINAL database.

        Returns the number of rows deleted.
        """
        if not keys:
            return 0
        conn = self._get_write_conn()
        self._begin(conn)
        try:
            total = 0
            for batch_start in range(0, len(keys), 500):
                batch = keys[batch_start:batch_start + 500]
                placeholders = ",".join("?" for _ in batch)
                cur = conn.execute(
                    f"DELETE FROM {table} WHERE key IN ({placeholders})", batch
                )
                total += cur.rowcount
            self._commit(conn)
            return total
        except Exception:
            self._rollback(conn)
            raise

    def delete_keys_by_prefix(self, prefix: str, table: str = "cursorDiskKV") -> int:
        """Delete all keys matching a prefix on the ORIGINAL database.

        Returns the number of rows deleted.
        """
        conn = self._get_write_conn()
        self._begin(conn)
        try:
            cur = conn.execute(
                f"DELETE FROM {table} WHERE key LIKE ?", (prefix + "%",)
            )
            self._commit(conn)
        except Exception:
            self._rollback(conn)
            raise
        return cur.rowcount



def snapshot_sqlite_db(src_path: Path, dest_path: Path) -> None:
    """Write a consistent single-file copy of a live SQLite database.

    Uses SQLite's online backup API through a read-only connection, so the
    copy reflects one committed state even while Cursor is writing (copying
    the main file and its -wal separately can capture two different states).
    """
    try:
        src = sqlite3.connect(f"{src_path.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        _copy_db_files(src_path, dest_path)
        return
    try:
        dest = sqlite3.connect(str(dest_path))
        try:
            src.backup(dest)
            dest.execute("PRAGMA journal_mode=DELETE")
        finally:
            dest.close()
    except sqlite3.OperationalError:
        dest_path.unlink(missing_ok=True)
        _copy_db_files(src_path, dest_path)
    finally:
        src.close()


def _copy_db_files(src_path: Path, dest_path: Path) -> None:
    """Fallback: raw-copy the database and its WAL, then fold the WAL in."""
    shutil.copy2(src_path, dest_path)
    for suffix in ("-wal", "-shm"):
        side = src_path.parent / (src_path.name + suffix)
        if side.exists():
            shutil.copy2(side, dest_path.parent / (dest_path.name + suffix))
    conn = sqlite3.connect(str(dest_path))
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.OperationalError:
        pass
    finally:
        conn.close()


def _backup_label(db_path: Path) -> str:
    if db_path.parent.name == "globalStorage":
        return "global"
    return f"workspace-{db_path.parent.name}"


def _prune_backups(backup_dir: Path, keep_recent: int, keep_daily: int) -> None:
    """Keep the newest `keep_recent` backups plus the first backup of each of
    the `keep_daily` most recent days, so repeated runs after a bad import
    can't rotate out the last known-good state."""
    backups = sorted(backup_dir.glob("state_*.vscdb"))  # names sort chronologically
    keep = set(backups[-keep_recent:]) if keep_recent > 0 else set()
    first_of_day: dict[str, Path] = {}
    for b in backups:
        first_of_day.setdefault(b.name[len("state_"):][:8], b)
    for day in sorted(first_of_day)[-keep_daily:] if keep_daily > 0 else []:
        keep.add(first_of_day[day])
    for b in backups:
        if b not in keep:
            b.unlink(missing_ok=True)


def backup_db(db_path: Path, keep_recent: int = 2, keep_daily: int = 3) -> Path:
    """Create a timestamped backup of a Cursor database.

    Backups go to ~/.local/share/cursaves/backups/<global|workspace-hash>/,
    outside Cursor's own storage and outside the ~/.cursaves sync repo.
    The global DB can be multi-GB, so retention is bounded: see
    _prune_backups.

    Returns the path to the new backup.
    """
    from datetime import datetime

    backup_dir = paths.get_backups_dir() / _backup_label(db_path)
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    backup_path = backup_dir / f"state_{timestamp}.vscdb"
    partial = backup_dir / f".{backup_path.name}.partial"
    try:
        snapshot_sqlite_db(db_path, partial)
        partial.replace(backup_path)
    finally:
        partial.unlink(missing_ok=True)

    _prune_backups(backup_dir, keep_recent, keep_daily)
    return backup_path
