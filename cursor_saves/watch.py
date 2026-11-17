"""Background daemon for automatic checkpoint + push to the sync backend."""

import hashlib
import os
import signal
import sys
import time
from datetime import datetime, timezone
from typing import Optional

from . import export, paths
from .backends import get_backend


def _get_db_fingerprint(project_path: str) -> Optional[str]:
    """Get a fingerprint of the current conversation state.

    Uses the modification time + size of the workspace and global DBs
    as a cheap change-detection signal.
    """
    parts = []

    # Global DB mtime + size
    global_db = paths.get_global_db_path()
    if global_db.exists():
        st = global_db.stat()
        parts.append(f"global:{st.st_mtime}:{st.st_size}")
        # Also check WAL file (most writes go here first)
        wal = global_db.parent / (global_db.name + "-wal")
        if wal.exists():
            wst = wal.stat()
            parts.append(f"wal:{wst.st_mtime}:{wst.st_size}")

    # Workspace DB mtime + size
    ws_dirs = paths.find_workspace_dirs_for_project(project_path)
    for ws_dir in ws_dirs[:1]:  # Just check the most recent
        ws_db = ws_dir / "state.vscdb"
        if ws_db.exists():
            st = ws_db.stat()
            parts.append(f"ws:{st.st_mtime}:{st.st_size}")

    if not parts:
        return None

    return hashlib.md5("|".join(parts).encode()).hexdigest()


def watch_loop(
    project_path: str,
    interval: int = 60,
    push: bool = True,
    verbose: bool = False,
):
    """Main watch loop -- polls for changes and auto-checkpoints.

    Args:
        project_path: The project to watch.
        interval: Seconds between checks.
        push: Whether to push checkpoints to the configured sync backend.
        verbose: Print status messages on every check.
    """
    backend = get_backend() if push else None
    if backend is not None and not backend.is_initialized():
        print("Warning: sync backend is not initialized (run `cursaves init`). Push disabled.")
        backend = None

    print("cursaves watch started")
    print(f"  Project: {project_path}")
    print(f"  Interval: {interval}s")
    print(f"  Push: {type(backend).__name__ if backend else 'disabled'}")
    print(f"  Machine: {paths.get_machine_id()}")
    print()

    snapshots_dir = paths.get_snapshots_dir()
    project_name = os.path.basename(os.path.normpath(project_path))
    last_fingerprint = _get_db_fingerprint(project_path)
    checkpoint_count = 0
    push_pending = False

    # Handle graceful shutdown
    running = True

    def handle_signal(signum, frame):
        nonlocal running
        print(f"\nShutting down (received signal {signum})...")
        running = False

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    while running:
        time.sleep(interval)

        if not running:
            break

        current_fingerprint = _get_db_fingerprint(project_path)

        if current_fingerprint != last_fingerprint:
            print(f"[{_now()}] change detected, checkpointing...")
            try:
                saved = export.checkpoint_project(project_path)
                if saved:
                    checkpoint_count += 1
                    push_pending = True
                    print(f"[{_now()}] checkpointed {len(saved)} conversation(s) (total: {checkpoint_count})")
                elif verbose:
                    print(f"[{_now()}] no conversations to checkpoint")
            except Exception as e:
                print(f"[{_now()}] error during checkpoint: {e}", file=sys.stderr)
            last_fingerprint = current_fingerprint
        elif verbose:
            print(f"[{_now()}] no changes detected")

        # A failed push stays pending and is retried on the next tick.
        if backend is not None and push_pending:
            timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            msg = f"[{paths.get_machine_id()}] checkpoint {project_name} ({timestamp})"
            try:
                ok = backend.push(snapshots_dir, msg)
            except Exception as e:
                print(f"[{_now()}] push error: {e}", file=sys.stderr)
                ok = False
            push_pending = not ok
            print(f"[{_now()}] push: {'ok' if ok else 'failed, will retry'}")

    print(f"\nwatch stopped. Total checkpoints: {checkpoint_count}")


def _now() -> str:
    """Return current time as a short string."""
    return datetime.now().strftime("%H:%M:%S")
