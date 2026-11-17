"""Platform detection and Cursor storage path resolution."""

import functools
import json
import os
import platform
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urlsplit


def get_cursor_user_dir() -> Path:
    """Return the Cursor User data directory for the current platform.

    macOS:  ~/Library/Application Support/Cursor/User
    Linux:  ~/.config/Cursor/User
    """
    system = platform.system()
    if system == "Darwin":
        base = Path.home() / "Library" / "Application Support" / "Cursor" / "User"
    elif system == "Linux":
        base = Path.home() / ".config" / "Cursor" / "User"
    else:
        print(
            f"Error: Unsupported platform '{system}'.\n"
            f"cursaves supports macOS and Linux.\n"
            f"On macOS, Cursor data is at ~/Library/Application Support/Cursor/User/\n"
            f"On Linux, Cursor data is at ~/.config/Cursor/User/",
            file=sys.stderr,
        )
        sys.exit(1)

    if not base.exists():
        print(
            f"Error: Cursor data directory not found at:\n"
            f"  {base}\n\n"
            f"This usually means:\n"
            f"  - Cursor is not installed on this machine, or\n"
            f"  - Cursor has never been opened (no data created yet), or\n"
            f"  - Cursor stores data at a non-standard location\n\n"
            f"Expected path for {system}: {base}",
            file=sys.stderr,
        )
        sys.exit(1)

    return base


def get_global_db_path() -> Path:
    """Return the path to Cursor's global state.vscdb."""
    return get_cursor_user_dir() / "globalStorage" / "state.vscdb"


def get_workspace_storage_dir() -> Path:
    """Return the path to Cursor's workspace storage directory."""
    return get_cursor_user_dir() / "workspaceStorage"


def _is_cursor_main_process(args: str, system: str) -> bool:
    """Return True if a `ps -o args` line is the main Cursor app process.

    Helper processes (renderer, GPU, utility, zygote) are excluded: on macOS
    they live under Frameworks/ and have "Helper" in their name; on Linux
    Electron marks them with a --type= argument.
    """
    if system == "Darwin":
        return (
            re.search(r"/Cursor(?: [^/]*)?\.app/Contents/MacOS/", args) is not None
            and "Helper" not in args
            and "Frameworks" not in args
        )
    if "--type=" in args:
        return False
    exe = os.path.basename(args.split()[0]).lower() if args.split() else ""
    return exe == "cursor" or (exe.startswith("cursor") and exe.endswith(".appimage"))


def is_cursor_running() -> bool:
    """Check if the main Cursor app process is running (macOS and Linux).

    `pgrep -x` is unreliable on macOS because the comm field is truncated
    to 16 characters, so we parse full command lines from `ps` instead.
    """
    try:
        result = subprocess.run(
            ["ps", "-axo", "args"],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return False
    if result.returncode != 0:
        return False
    system = platform.system()
    return any(
        _is_cursor_main_process(line.strip(), system)
        for line in result.stdout.splitlines()
    )


def get_cursor_projects_dir() -> Path:
    """Return the path to ~/.cursor/projects/ (agent transcripts, etc.)."""
    return Path.home() / ".cursor" / "projects"


def sanitize_project_path(project_path: str) -> str:
    """Convert a project path to Cursor's sanitized directory name format.

    /Users/callum/Desktop/Projects/myrepo -> Users-callum-Desktop-Projects-myrepo
    """
    # Strip leading slash and replace / with -
    return project_path.strip("/").replace("/", "-")


def _decode_ssh_host(host: str) -> str:
    """Decode an SSH host identifier.

    Cursor encodes SSH hosts as hex-encoded JSON, e.g.:
    7b22686f73744e616d65223a22636f7265227d -> {"hostName":"core"} -> core
    """
    try:
        # Try to decode as hex
        decoded = bytes.fromhex(host).decode("utf-8")
        # Try to parse as JSON
        data = json.loads(decoded)
        if isinstance(data, dict) and "hostName" in data:
            return data["hostName"]
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        pass
    return host


def uri_to_path(uri: str) -> Optional[str]:
    """Filesystem path from a workspace.json file:// or vscode-remote:// URI.

    Paths are percent-encoded in these URIs (spaces, '#', non-ASCII, ...).
    Returns None for other schemes.
    """
    if uri.startswith(("file://", "vscode-remote://")):
        return unquote(urlsplit(uri).path) or "/"
    return None


def find_workspace_dirs_for_project(project_path: str) -> list[Path]:
    """Find all workspace directories that map to a given project path.

    Scans workspace.json files in workspaceStorage/ to find matches.
    Returns list of workspace directory paths, newest first.
    """
    ws_storage = get_workspace_storage_dir()
    if not ws_storage.exists():
        return []

    # Normalise the target path for comparison
    target = os.path.normpath(os.path.expanduser(project_path))

    matches = []
    for ws_dir in ws_storage.iterdir():
        if not ws_dir.is_dir():
            continue
        ws_json = ws_dir / "workspace.json"
        if not ws_json.exists():
            continue
        try:
            data = json.loads(ws_json.read_text())
            folder_path = uri_to_path(data.get("folder", ""))
            if folder_path is None:
                continue

            if os.path.normpath(folder_path) == target:
                matches.append(ws_dir)
        except (json.JSONDecodeError, OSError):
            continue

    # Sort by modification time, newest first
    matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return matches


def find_transcript_dir(project_path: str) -> Optional[Path]:
    """Find the agent-transcripts directory for a project."""
    projects_dir = get_cursor_projects_dir()
    if not projects_dir.exists():
        return None

    sanitized = sanitize_project_path(project_path)
    transcript_dir = projects_dir / sanitized / "agent-transcripts"
    if transcript_dir.exists():
        return transcript_dir

    return None


def get_project_path() -> str:
    """Get the current project path (current working directory)."""
    return os.getcwd()


def list_all_workspaces() -> list[dict]:
    """List all Cursor workspaces with metadata.

    Returns a list of dicts with:
      - folder_uri: raw URI from workspace.json
      - path: extracted filesystem path (for workspace, path to the .code-workspace file)
      - type: 'local', 'ssh', or 'workspace'
      - host: SSH hostname (for ssh type, None otherwise)
      - workspace_dir: Path to the workspace directory
      - mtime: modification time of the workspace DB
    """
    ws_storage = get_workspace_storage_dir()
    if not ws_storage.exists():
        return []

    workspaces = []
    for ws_dir in ws_storage.iterdir():
        if not ws_dir.is_dir():
            continue
        ws_json = ws_dir / "workspace.json"
        if not ws_json.exists():
            continue
        try:
            data = json.loads(ws_json.read_text())

            ws_type = "local"
            host = None
            folder_path = ""
            folder_uri = ""

            # workspace .code-workspace: uses "workspace" key instead of "folder"
            if "workspace" in data and not data.get("folder"):
                ws_uri = data["workspace"]
                if ws_uri.startswith("file://"):
                    folder_uri = ws_uri
                    folder_path = uri_to_path(ws_uri)
                    ws_type = "workspace"
                else:
                    continue
            else:
                folder_uri = data.get("folder", "")
                if not folder_uri:
                    continue

                folder_path = uri_to_path(folder_uri)
                if folder_path is None:
                    continue
                if folder_uri.startswith("vscode-remote://"):
                    ws_type = "ssh"
                    # Format: vscode-remote://ssh-remote%2B<host>/<path>
                    authority = unquote(urlsplit(folder_uri).netloc)
                    if "+" in authority:
                        host = authority.split("+", 1)[1]
                    # Decode the host if it's hex-encoded JSON (e.g. {"hostName":"core"})
                    if host:
                        host = _decode_ssh_host(host)

            # Get DB modification time
            db_path = ws_dir / "state.vscdb"
            mtime = db_path.stat().st_mtime if db_path.exists() else 0

            workspaces.append({
                "folder_uri": folder_uri,
                "path": os.path.normpath(folder_path),
                "type": ws_type,
                "host": host,
                "workspace_dir": ws_dir,
                "mtime": mtime,
            })
        except (json.JSONDecodeError, OSError):
            continue

    # Sort by modification time, newest first
    workspaces.sort(key=lambda w: w["mtime"], reverse=True)
    return workspaces


_global_headers_cache: Optional[dict[str, list[dict]]] = None


def _build_global_headers_map() -> dict[str, list[dict]]:
    """Build a workspace-hash → [composer header entries] map from the global DB.

    Sources: the composer.composerHeaders index (Cursor 3.0-era builds) and
    each chat's own composerData.workspaceIdentifier (current builds, which
    have no central index). Index entries win when both exist.

    Returns a dict keyed by workspace directory hash (workspaceIdentifier.id).
    Cached for the lifetime of the process.
    """
    from . import db

    global _global_headers_cache
    if _global_headers_cache is not None:
        return _global_headers_cache

    result: dict[str, list[dict]] = {}
    global_db = get_global_db_path()
    if global_db.exists():
        try:
            with db.CursorDB(global_db) as cdb:
                headers = cdb.get_json("composer.composerHeaders", table="ItemTable")
                indexed = headers.get("allComposers", []) if isinstance(headers, dict) else []
                derived = cdb.composer_index()
        except Exception:
            indexed, derived = [], []
        seen: set[str] = set()
        for entry in indexed + derived:
            cid = entry.get("composerId")
            ws_id = (entry.get("workspaceIdentifier") or {}).get("id", "")
            if cid and ws_id and cid not in seen:
                seen.add(cid)
                result.setdefault(ws_id, []).append(entry)
    _global_headers_cache = result
    return result


def invalidate_headers_cache():
    """Clear the cached global headers map (call after writing to the global DB)."""
    global _global_headers_cache
    _global_headers_cache = None


def get_workspace_composer_ids(ws_db_path: Path) -> list[str]:
    """Extract all composer IDs associated with a workspace.

    Combines multiple sources for maximum coverage:
    1. Global composer.composerHeaders index (Cursor 3.0+, most authoritative
       but only contains recently-active chats)
    2. Workspace DB selectedComposerIds + composerChatViewPane entries
       (catches chats opened before the 3.0 migration that aren't yet
       in the global index)
    3. Workspace DB allComposers (Cursor 2.x fallback)

    Returns deduplicated IDs.
    """
    from . import db

    ids: set[str] = set()
    ws_hash = ws_db_path.parent.name

    # Source 1: global headers index (Cursor 3.0+)
    headers_map = _build_global_headers_map()
    for entry in headers_map.get(ws_hash, []):
        cid = entry.get("composerId")
        if cid:
            ids.add(cid)

    # Source 2+3: workspace DB
    try:
        with db.CursorDB(ws_db_path) as cdb:
            data = cdb.get_json("composer.composerData", table="ItemTable")
            if not data:
                return list(ids)

            # Cursor 2.x: allComposers (complete list for old workspaces)
            for c in data.get("allComposers", []):
                cid = c.get("composerId")
                if cid:
                    ids.add(cid)

            # Cursor 3.0+: supplementary sources for chats not in global index
            for cid in data.get("selectedComposerIds", []):
                if cid:
                    ids.add(cid)
            for cid in data.get("lastFocusedComposerIds", []):
                if cid:
                    ids.add(cid)

            for key in cdb.list_keys(
                "workbench.panel.composerChatViewPane.", table="ItemTable"
            ):
                pane = cdb.get_json(key, table="ItemTable")
                if isinstance(pane, dict):
                    for view_key in pane:
                        if ".view." in view_key:
                            cid = view_key.rsplit(".", 1)[-1]
                            if cid:
                                ids.add(cid)
    except Exception:
        pass

    return list(ids)


def list_workspaces_with_conversations() -> list[dict]:
    """List workspaces that have at least one conversation.

    Returns the same dicts as list_all_workspaces(), plus a
    'conversations' key with the count.
    """
    result = []
    for ws in list_all_workspaces():
        db_path = ws["workspace_dir"] / "state.vscdb"
        if not db_path.exists():
            continue
        composer_ids = get_workspace_composer_ids(db_path)
        if composer_ids:
            ws["conversations"] = len(composer_ids)
            result.append(ws)
    # Stable order (not last-modified) so a number shown by `cursaves
    # workspaces` still refers to the same workspace in the next command.
    result.sort(key=lambda w: (w["path"], w.get("host") or "", w["workspace_dir"].name))
    return result


def resolve_workspace(selector: str) -> Optional[dict]:
    """Resolve a workspace selector to a workspace dict.

    The selector can be:
      - A number (1-based index from list_workspaces_with_conversations)
      - A workspace hash (directory name under workspaceStorage/)
      - A path substring (matched against workspace paths)
    """
    workspaces = list_workspaces_with_conversations()

    # Try as index
    try:
        idx = int(selector)
        if workspaces and 1 <= idx <= len(workspaces):
            return workspaces[idx - 1]
        return None
    except ValueError:
        pass

    # Try as workspace hash (exact match, or prefix match when selector is 8 chars (short hash))
    # Allow the short hash because that's what's displayed in the workspaces list,
    # so user can just copy-paste the short hash, e.g. `cursaves push -w 497e8ab0`
    for ws in workspaces:
        name = ws["workspace_dir"].name
        if len(selector) == 8:
            # Short hash match (8 chars) - allow prefix match
            if name.startswith(selector):
                return ws
        else:
            # Exact match
            if name == selector:
                return ws

    # Try as exact path, then as an unambiguous path substring
    normalized = os.path.normpath(os.path.expanduser(selector))
    exact = [ws for ws in workspaces if ws["path"] == normalized]
    if len(exact) == 1:
        return exact[0]
    matches = exact or [ws for ws in workspaces if selector in ws["path"]]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        print(f"'{selector}' matches {len(matches)} workspaces:", file=sys.stderr)
        for ws in matches:
            print(f"  {ws['workspace_dir'].name[:8]}  {format_workspace_display(ws)}", file=sys.stderr)
        print("Use the hash to pick one.", file=sys.stderr)

    return None


def get_sync_dir() -> Path:
    """Return the cursaves sync directory (~/.cursaves/).

    This is the git repo that holds snapshots and is synced between machines.
    """
    return Path.home() / ".cursaves"


def get_backups_dir() -> Path:
    """Return the directory for Cursor DB backups.

    Deliberately outside ~/.cursaves/: the git backend runs `git clean`
    there, which would delete untracked backups.
    """
    return Path.home() / ".local" / "share" / "cursaves" / "backups"


def get_snapshots_dir() -> Path:
    """Return the snapshots directory (~/.cursaves/snapshots/)."""
    snapshots = get_sync_dir() / "snapshots"
    snapshots.mkdir(parents=True, exist_ok=True)
    return snapshots


def is_sync_repo_initialized() -> bool:
    """Check if a sync backend has been configured (git repo or cloud)."""
    sync_dir = get_sync_dir()
    if (sync_dir / ".git").exists():
        return True
    # Check for non-git backend config
    config_path = Path.home() / ".config" / "cursaves" / "config.json"
    if config_path.exists():
        try:
            import json
            cfg = json.loads(config_path.read_text())
            return cfg.get("backend") in ("s3", "azure")
        except Exception:
            pass
    return False


def get_machine_id() -> str:
    """Return a human-readable machine identifier."""
    import socket

    return socket.gethostname()


# ── Workspace matching for imports ─────────────────────────────────────


def find_all_matching_workspaces(source_path: str) -> list[dict]:
    """Find all workspaces that could receive imports from source_path.

    Matches by:
    1. Exact path match (for SSH workspaces with same remote path)
    2. Same basename (fallback for different directory structures)

    Returns list of workspace dicts with type, host, path, workspace_dir,
    sorted by match quality (exact matches first) then by mtime.
    """
    all_ws = list_all_workspaces()
    source_normalized = os.path.normpath(source_path)
    source_basename = os.path.basename(source_normalized)

    exact_matches = []
    basename_matches = []

    for ws in all_ws:
        ws_path = ws["path"]
        ws_basename = os.path.basename(ws_path)

        if ws_path == source_normalized:
            exact_matches.append(ws)
        elif ws_basename == source_basename:
            basename_matches.append(ws)

    # Return exact matches first, then basename matches
    return exact_matches + basename_matches


def format_workspace_display(ws: dict, include_path: bool = True) -> str:
    """Format a workspace dict for display.

    Returns a string like "ssh core /mnt/home/.../project", "(local) /home/.../project",
    or "(workspace) /home/.../my-proj.code-workspace"
    """
    if ws["type"] == "ssh":
        host = ws.get("host") or "unknown"
        if include_path:
            path = ws["path"]
            if len(path) > 40:
                path = "..." + path[-37:]
            return f"ssh {host} {path}"
        return f"ssh {host}"
    elif ws["type"] == "workspace":
        if include_path:
            path = ws["path"]
            if len(path) > 45:
                path = "..." + path[-42:]
            return f"(workspace) {path}"
        return "(workspace)"
    else:
        if include_path:
            path = ws["path"]
            if len(path) > 45:
                path = "..." + path[-42:]
            return f"(local) {path}"
        return "(local)"


# ── Project identification ────────────────────────────────────────────


def get_project_identifier(project_path: str) -> str:
    """Get a stable identifier for a project, used as the snapshot subdirectory.

    Uses the git remote origin URL if available (normalized to a filesystem-safe
    string).  Falls back to the directory basename for non-git projects.

    This means:
      - Same repo under different local names (bob/ vs alice/) → same identifier
      - Different repos that happen to share a name → different identifiers
    """
    remote_url = _get_git_remote_url(project_path)
    if remote_url:
        return _normalize_remote_url(remote_url)
    return os.path.basename(os.path.normpath(project_path))


@functools.lru_cache(maxsize=None)
def remote_project_identifier(project_path: str) -> Optional[str]:
    """Project identifier from the git remote, or None if there isn't one
    (not a git repo, no origin, or the path doesn't exist on this machine)."""
    url = _get_git_remote_url(project_path)
    return _normalize_remote_url(url) if url else None


def _get_git_remote_url(project_path: str) -> Optional[str]:
    """Get the git remote origin URL for a project, if any."""
    try:
        result = subprocess.run(
            ["git", "-C", project_path, "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


def _normalize_remote_url(url: str) -> str:
    """Normalize a git remote URL to a stable, filesystem-safe directory name.

    git@github.com:user/repo.git                → github.com-user-repo
    https://github.com/user/repo.git            → github.com-user-repo
    ssh://git@github.com/user/repo              → github.com-user-repo
    https://user:token@github.com/user/repo.git → github.com-user-repo
    ssh://git@host:2222/user/repo               → host-user-repo

    Credentials and ports are always dropped: the identifier becomes a
    directory name that is committed and pushed to the sync remote.
    """
    url = url.strip().rstrip("/")
    url = re.sub(r"\.git$", "", url)

    # URL form: scheme://[user[:password]@]host[:port]/path
    if "://" in url:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        path = parsed.path.strip("/")
        if host:
            return _sanitize_identifier(f"{host}/{path}")
        # e.g. file:///srv/git/repo
        return _sanitize_identifier(path)

    # scp-like SSH shorthand: [user@]host:path
    m = re.match(r"^(?:[^@/]+@)?([\w.-]+):(?!/)(.*)", url)
    if m:
        host, path = m.group(1).lower(), m.group(2)
        return _sanitize_identifier(f"{host}/{path}")

    # Local path or unknown format
    return _sanitize_identifier(url)


def _sanitize_identifier(s: str) -> str:
    """Turn an arbitrary string into a safe directory name.

    Replaces slashes, colons, @, etc. with '-' and collapses runs of dashes.
    """
    s = re.sub(r"[/:@\\]+", "-", s)
    s = re.sub(r"-+", "-", s)
    return s.strip("-")
