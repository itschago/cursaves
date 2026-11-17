"""Sync backends for cursaves snapshot storage.

Each backend handles the transport layer: syncing snapshot files between
the local ``~/.cursaves/snapshots/`` directory and a remote store (git repo,
S3 bucket, Azure container, etc.).

The local snapshots directory is always the source of truth for reads —
backends just keep it in sync with a remote.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional


_CONFIG_PATH = Path.home() / ".config" / "cursaves" / "config.json"


# ── Abstract base ────────────────────────────────────────────────────────


class SyncBackend(ABC):
    """Interface every sync backend must implement."""

    @abstractmethod
    def pull(self, snapshots_dir: Path) -> bool:
        """Download remote snapshots into *snapshots_dir*.

        Must be idempotent — running twice without changes is a no-op.
        Returns True on success, False on failure.
        """

    @abstractmethod
    def push(self, snapshots_dir: Path, message: Optional[str] = None) -> bool:
        """Upload local snapshots from *snapshots_dir* to the remote.

        *message* describes the change where the backend records history.
        Returns True on success, False on failure.
        """

    @abstractmethod
    def has_remote(self) -> bool:
        """Return True if a remote target is configured."""

    @abstractmethod
    def is_initialized(self) -> bool:
        """Return True if the backend has been set up (init already run)."""


# ── Git backend ──────────────────────────────────────────────────────────


class GitBackend(SyncBackend):
    """Original backend: a local git repo at *sync_dir* with an optional remote."""

    def __init__(self, sync_dir: Path):
        self.sync_dir = sync_dir

    # -- SyncBackend interface ------------------------------------------

    PUSH_ATTEMPTS = 3

    def pull(self, snapshots_dir: Path) -> bool:
        if not self.has_remote():
            return True
        return self._sync_with_origin()

    def push(self, snapshots_dir: Path, message: Optional[str] = None) -> bool:
        self._commit_local_changes(message)
        if not self.has_remote():
            return True

        for attempt in range(1, self.PUSH_ATTEMPTS + 1):
            try:
                result = self._git(
                    "push", "--no-verify", "-u", "origin", "HEAD:main", timeout=120,
                )
            except subprocess.TimeoutExpired:
                print("  Push timed out", file=sys.stderr)
                return False
            if result.returncode == 0:
                return True
            rejected = any(
                s in result.stderr for s in ("rejected", "non-fast-forward", "fetch first")
            )
            if not rejected or attempt == self.PUSH_ATTEMPTS:
                print(f"  Push failed: {result.stderr.strip()}", file=sys.stderr)
                return False
            # Another machine pushed first: bring in its changes, keep ours, retry.
            if not self._sync_with_origin():
                return False
        return False

    def has_remote(self) -> bool:
        try:
            result = subprocess.run(
                ["git", "remote"],
                capture_output=True, text=True,
                cwd=str(self.sync_dir),
            )
            return result.returncode == 0 and result.stdout.strip() != ""
        except FileNotFoundError:
            return False

    def is_initialized(self) -> bool:
        return (self.sync_dir / ".git").exists()

    # -- Git-specific helpers -------------------------------------------

    def _git(self, *args: str, timeout: Optional[float] = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args], cwd=str(self.sync_dir),
            capture_output=True, text=True, timeout=timeout,
        )

    def _rev(self, ref: str) -> Optional[str]:
        result = self._git("rev-parse", "--verify", "-q", ref)
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None

    def _tree_blobs(self, rev: Optional[str]) -> dict[str, str]:
        """Map snapshots/ paths to blob ids at a commit ({} for None)."""
        if rev is None:
            return {}
        out = self._git("ls-tree", "-r", "-z", "--full-tree", rev, "--", "snapshots/").stdout
        blobs = {}
        for entry in filter(None, out.split("\0")):
            meta, path = entry.split("\t", 1)
            blobs[path] = meta.split()[2]
        return blobs

    def _commit_local_changes(self, message: Optional[str] = None) -> bool:
        """Commit any snapshot changes in the working tree. Returns True if committed."""
        self._git("add", "-A", "snapshots/")
        if self._git("diff", "--cached", "--quiet").returncode == 0:
            return False
        from . import paths
        self._git(
            "commit", "--no-verify", "-m",
            message or f"[{paths.get_machine_id()}] sync snapshots",
        )
        return True

    def _sync_with_origin(self) -> bool:
        """Fetch origin/main and move onto it, carrying over local snapshot changes.

        Local changes (unpushed commits and uncommitted files) are re-applied
        one chat at a time. If the remote changed the same chat since the
        last sync, the remote version is kept: snapshots are regenerated from
        Cursor's DB, so the local chat is re-evaluated on the next sync.
        """
        import shutil
        import tempfile

        if not self.sync_dir.exists():
            return False

        for abort_cmd in (("rebase", "--abort"), ("merge", "--abort"), ("cherry-pick", "--abort")):
            self._git(*abort_cmd)

        base = self._rev("refs/remotes/origin/main")
        try:
            fetch = self._git("fetch", "--depth", "1", "origin", timeout=180)
        except subprocess.TimeoutExpired:
            return False
        if fetch.returncode != 0:
            heads = self._git("ls-remote", "--heads", "origin", timeout=60)
            return heads.returncode == 0 and not heads.stdout.strip()  # empty remote
        remote = self._rev("refs/remotes/origin/main")
        if remote is None:
            return True  # empty remote: nothing to pull

        # Everything that differs locally from the last-synced remote state
        self._git("add", "-A", "snapshots/")
        if base is not None:
            out = self._git(
                "diff", "--cached", "--name-status", "--no-renames", "-z", base, "--", "snapshots/",
            ).stdout
            fields = [f for f in out.split("\0") if f]
            changes = list(zip(fields[1::2], fields[0::2]))  # (path, status)
        else:
            out = self._git("ls-files", "-z", "--", "snapshots/").stdout
            changes = [(p, "A") for p in out.split("\0") if p]

        stash = Path(tempfile.mkdtemp(prefix="cursaves-git-"))
        try:
            for path, status in changes:
                if status != "D":
                    dest = stash / path
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(self.sync_dir / path, dest)

            self._git("checkout", "-f", "-B", "main", "refs/remotes/origin/main")
            self._git("reset", "--hard", "refs/remotes/origin/main")
            self._git("branch", "--set-upstream-to=origin/main", "main")
            self._git("clean", "-fd", "--", "snapshots/")

            base_blobs, remote_blobs = self._tree_blobs(base), self._tree_blobs(remote)

            def chat_key(path: str) -> str:
                parent, _, name = path.rpartition("/")
                return f"{parent}/{name.split('.', 1)[0]}"

            groups: dict[str, list[tuple[str, str]]] = {}
            for path, status in changes:
                groups.setdefault(chat_key(path), []).append((path, status))

            kept_remote = 0
            for key, items in groups.items():
                # every file of this chat at either revision
                group_paths = {p for p in set(base_blobs) | set(remote_blobs) if chat_key(p) == key}
                if any(base_blobs.get(p) != remote_blobs.get(p) for p in group_paths):
                    kept_remote += 1
                    continue
                for path, status in items:
                    target = self.sync_dir / path
                    if status == "D":
                        target.unlink(missing_ok=True)
                    else:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(stash / path, target)
        finally:
            shutil.rmtree(stash, ignore_errors=True)

        if kept_remote:
            print(
                f"  {kept_remote} chat(s) changed both here and on the remote since the last "
                f"sync; kept the remote snapshot (local chats are re-checked on the next sync).",
                file=sys.stderr,
            )
        self._commit_local_changes()
        return True

    def init_repo(self, remote: Optional[str] = None):
        """Create the git repo and optionally add a remote."""
        self.sync_dir.mkdir(parents=True, exist_ok=True)
        (self.sync_dir / "snapshots").mkdir(exist_ok=True)

        subprocess.run(
            ["git", "init", "-b", "main"],
            cwd=str(self.sync_dir), capture_output=True,
        )

        gitignore = self.sync_dir / ".gitignore"
        gitignore.write_text(".DS_Store\n")

        subprocess.run(
            ["git", "add", "."],
            cwd=str(self.sync_dir), capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "--no-verify", "-m", "Initialize cursaves sync repo"],
            cwd=str(self.sync_dir), capture_output=True,
        )

        if remote:
            subprocess.run(
                ["git", "remote", "add", "origin", remote],
                cwd=str(self.sync_dir), capture_output=True,
            )

    def update_remote(self, remote: str):
        """Add or update the origin remote."""
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=str(self.sync_dir),
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            subprocess.run(
                ["git", "remote", "set-url", "origin", remote],
                cwd=str(self.sync_dir), capture_output=True,
            )
        else:
            subprocess.run(
                ["git", "remote", "add", "origin", remote],
                cwd=str(self.sync_dir), capture_output=True,
            )


# ── S3 backend ───────────────────────────────────────────────────────────


class S3Backend(SyncBackend):
    """Sync snapshots to/from an S3 bucket.

    Requires ``boto3`` — install with ``pip install cursaves[s3]``.

    Configuration (in ~/.config/cursaves/config.json)::

        {
            "backend": "s3",
            "s3": {
                "bucket": "my-cursor-saves",
                "prefix": "snapshots/",
                "region": "us-east-1"      // optional
            }
        }

    Authentication uses the standard AWS credential chain:
    env vars, ~/.aws/credentials, IAM roles, etc.
    """

    def __init__(self, bucket: str, prefix: str = "snapshots/", region: Optional[str] = None):
        self.bucket = bucket
        self.prefix = prefix.rstrip("/") + "/"
        self.region = region
        self._client = None

    def _get_client(self):
        if self._client is None:
            try:
                import boto3
            except ImportError:
                print(
                    "Error: boto3 is required for S3 backend.\n"
                    "Install it with: pip install cursaves[s3]  or  pip install boto3",
                    file=sys.stderr,
                )
                sys.exit(1)
            kwargs = {}
            if self.region:
                kwargs["region_name"] = self.region
            self._client = boto3.client("s3", **kwargs)
        return self._client

    # -- SyncBackend interface ------------------------------------------

    def pull(self, snapshots_dir: Path) -> bool:
        """Download all remote snapshot files that are newer or missing locally."""
        client = self._get_client()
        try:
            paginator = client.get_paginator("list_objects_v2")
            pages = paginator.paginate(Bucket=self.bucket, Prefix=self.prefix)

            downloaded = 0
            for page in pages:
                for obj in page.get("Contents", []):
                    remote_key = obj["Key"]
                    rel_path = remote_key[len(self.prefix):]
                    if not rel_path:
                        continue

                    local_path = snapshots_dir / rel_path
                    remote_mtime = obj["LastModified"].timestamp()

                    if local_path.exists():
                        local_mtime = local_path.stat().st_mtime
                        local_size = local_path.stat().st_size
                        if local_size == obj["Size"] and local_mtime >= remote_mtime:
                            continue

                    local_path.parent.mkdir(parents=True, exist_ok=True)
                    client.download_file(self.bucket, remote_key, str(local_path))
                    os.utime(str(local_path), (remote_mtime, remote_mtime))
                    downloaded += 1

            if downloaded:
                print(f"  Downloaded {downloaded} file(s) from s3://{self.bucket}")
            return True

        except Exception as e:
            print(f"S3 pull failed: {e}", file=sys.stderr)
            return False

    def push(self, snapshots_dir: Path, message: Optional[str] = None) -> bool:
        """Upload local snapshot files that are newer or missing remotely."""
        client = self._get_client()
        try:
            # Build index of remote objects
            remote_index: dict[str, tuple[float, int]] = {}
            paginator = client.get_paginator("list_objects_v2")
            pages = paginator.paginate(Bucket=self.bucket, Prefix=self.prefix)
            for page in pages:
                for obj in page.get("Contents", []):
                    rel = obj["Key"][len(self.prefix):]
                    if rel:
                        remote_index[rel] = (obj["LastModified"].timestamp(), obj["Size"])

            uploaded = 0
            for local_path in snapshots_dir.rglob("*"):
                if not local_path.is_file():
                    continue
                rel = str(local_path.relative_to(snapshots_dir))
                remote_key = self.prefix + rel

                local_mtime = local_path.stat().st_mtime
                local_size = local_path.stat().st_size

                if rel in remote_index:
                    remote_mtime, remote_size = remote_index[rel]
                    if local_size == remote_size and local_mtime <= remote_mtime:
                        continue

                client.upload_file(str(local_path), self.bucket, remote_key)
                uploaded += 1

            if uploaded:
                print(f"  Uploaded {uploaded} file(s) to s3://{self.bucket}")
            return True

        except Exception as e:
            print(f"S3 push failed: {e}", file=sys.stderr)
            return False

    def has_remote(self) -> bool:
        return True  # S3 is always remote

    def is_initialized(self) -> bool:
        try:
            client = self._get_client()
            client.head_bucket(Bucket=self.bucket)
            return True
        except Exception:
            return False


# ── Configuration ────────────────────────────────────────────────────────


def load_config() -> dict:
    """Load cursaves config from ~/.config/cursaves/config.json."""
    if _CONFIG_PATH.exists():
        try:
            return json.loads(_CONFIG_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_config(config: dict):
    """Persist cursaves config."""
    _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CONFIG_PATH.write_text(json.dumps(config, indent=2) + "\n")


def get_backend() -> SyncBackend:
    """Instantiate the configured sync backend.

    Falls back to GitBackend if nothing is configured (backward-compatible).
    """
    from . import paths

    config = load_config()
    backend_type = config.get("backend", "git")

    if backend_type == "s3":
        s3_cfg = config.get("s3", {})
        bucket = s3_cfg.get("bucket")
        if not bucket:
            print("Error: S3 backend configured but no bucket specified.", file=sys.stderr)
            print("Run: cursaves init --backend s3 --bucket <name>", file=sys.stderr)
            sys.exit(1)
        return S3Backend(
            bucket=bucket,
            prefix=s3_cfg.get("prefix", "snapshots/"),
            region=s3_cfg.get("region"),
        )

    # Default: git
    return GitBackend(paths.get_sync_dir())
