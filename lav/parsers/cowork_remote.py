#!/usr/bin/env python3
"""
Cowork remote / Claude Code cloud parser for LocalAgentViewer (LAV-94).

Since 2026-07-31 Cowork sessions are logged in the cloud: the Mac runs the commands
in its VM, but transcript and usage live on Anthropic's side, and the local
audit.jsonl files that `cowork_desktop` reads stop there. The only way to get them
back is `claude --teleport <id> -p "/cost"`: it writes the full transcript, in the
standard Claude Code JSONL format, WITHOUT calling the model.

Three steps, kept apart on purpose:

1. **Discover** (pure, read-only): cloud session ids from Claude Desktop's
   `remote-session-spaces.json` and logs. `cse_X`, `session_X` and `rcw-x`
   (lowercased) are the SAME session. Remote-control sessions are LOCAL sessions
   mirrored to claude.ai (already in LAV as claude_code): they are excluded.
2. **Fetch** (opt-in, the only step that runs an external program): teleport from
   a LAV-owned empty dir, then MOVE the transcript out of ~/.claude/projects into
   `cowork-remote/sessions/<remote_id>.jsonl` next to a `.meta.json` sidecar. The
   claude_code parser never sees it: see `config.is_lav_internal_project_dir`.
   A session Desktop logged activity on after its last fetch is `stale` and is
   teleported again; the new file replaces the stored one only if it holds all of
   its cloud lines (their uuid is stable across teleports).
3. **Parse** (pure, idempotent): every fetched transcript, every run. No watermark.

What the teleported file gets wrong, and the parser corrects:
  - `sessionId` is a NEW local uuid  -> the remote id from the sidecar.
  - `cwd` is the teleport dir on EVERY line -> the session's first granted folder.
  - lines added by the teleport itself (resume notice, /cost, a real prompt if one
    was sent) are mixed in -> only `remoteSourced: true` records are kept.
"""

import argparse
import getpass
import hashlib
import json
import os
import platform
import re
import shutil
import signal
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from lav.config import (
    CLAUDE_DESKTOP_LOGS_DIR,
    COWORK_REMOTE_DIR,
    COWORK_REMOTE_SESSIONS_DIR,
    COWORK_REMOTE_TELEPORT_CWD,
    COWORK_SESSIONS_DIRS_DEFAULT,
    SOURCE_COWORK_REMOTE,
    get_cowork_remote_spaces_files,
    is_lav_internal_project_dir,
    load_runtime_config,
)
from lav.parsers.jsonl import (
    init_db,
    parse_jsonl_file,
    upsert_session_source,
    process_message_content,
    process_tool_results,
    extract_tool_calls,
    process_tool_call,
    process_token_usage,
    flush_pending_tool_results,
    update_interaction,
    get_or_create_project,
    get_or_create_user,
    get_or_create_host,
    infer_cowork_project,
    dropped_tool_results,
    notify_collector,
    _canonical_hostname,
)


# Same bucket as cowork_desktop: the Source dimension already tells them apart.
DEFAULT_PROJECT = "cowork_default"
STDIO_TAIL = 2000

# ===========================================================================
# DISCOVERY
# ===========================================================================

_LOG_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
_CLOUD_ID_RE = re.compile(r"\b(?:cse|session)_(01[A-Za-z0-9]{20,30})\b")
# The VM user is the session suffix LOWERCASED: it can only be resolved
# case-insensitively against an id seen in its real form somewhere else.
_RCW_RE = re.compile(r"\brcw-(01[a-z0-9]{20,30})\b")
# Desktop's remote_cowork.* lines name the session only as
# sha256(lower("cse_" + suffix)): reproducible, so it maps back to a known id.
_DIGEST_RE = re.compile(r'"sessionIdDigest":"sha256_([0-9a-f]{64})"')
_LOG_GLOBS = ("main*.log", "claude.ai-web*.log")

TAG_SPACES = "spaces"
TAG_REMOTE_CONTROL = "remote-control"
TAG_BRIDGE = "bridge"
# Families that mean "Cowork". Anything with a [sessions-api] line is Dispatch;
# the rest (focused in Desktop's Code tab) is Claude Code cloud.
_COWORK_TAGS = {TAG_SPACES, "vm", "grant", "clear-grant", "web-cowork"}


def _line_tag(line: str) -> str:
    if "Remote control enabled" in line:
        return TAG_REMOTE_CONTROL
    if "[remote-bash]" in line:
        return "vm"
    if "clearRemoteSessionFolderGrants" in line:
        return "clear-grant"
    if "grantRemoteSessionFolders" in line:
        return "grant"
    if "[sessions-api]" in line or "sessions-bridge" in line:
        return "dispatch"
    if "setFocusedSession" in line:
        return "ccd-focus"
    if "remote_cowork" in line:
        return "web-cowork"
    return "other"


def _log_ts_utc(line: str) -> Optional[str]:
    """Desktop logs carry LOCAL wall time with no offset."""
    m = _LOG_TS_RE.match(line)
    if not m:
        return None
    local = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").astimezone()
    return local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def session_digest(remote_id: str) -> str:
    """The digest Desktop logs for a cloud session (see _DIGEST_RE)."""
    suffix = remote_id.split("_", 1)[-1]
    return hashlib.sha256(f"cse_{suffix}".lower().encode()).hexdigest()


def _touch(info: dict, ts: Optional[str]) -> None:
    if ts:
        info["first_seen"] = min(filter(None, (info["first_seen"], ts)))
        info["last_seen"] = max(filter(None, (info["last_seen"], ts)))


def normalize_remote_id(value: str) -> str:
    """`cse_X`, `session_X` or a claude.ai URL -> `session_X`. Anything else as is."""
    value = (value or "").strip().rstrip("/")
    m = _CLOUD_ID_RE.search(value)
    return f"session_{m.group(1)}" if m else value


def discover(spaces_files: Optional[list] = None, logs_dir: Optional[Path] = None,
             desktop_session_dirs: Optional[list] = None) -> tuple:
    """Every cloud session id Claude Desktop knows about, with where it was seen.

    Returns ({remote_id: info}, {digest: (first_seen, last_seen)}).
    `info["excluded"]` is a non-empty reason when the id must NOT be fetched
    automatically. The second dict holds sessions Desktop logged activity on
    whose id never appeared in clear anywhere: they cannot be fetched.
    """
    found: dict = {}

    def _entry(rid: str) -> dict:
        return found.setdefault(rid, {
            "remote_id": rid, "tags": set(), "folders": [], "memory_enabled": None,
            "first_seen": None, "last_seen": None,
        })

    for f in (spaces_files if spaces_files is not None else get_cowork_remote_spaces_files()):
        try:
            entries = json.loads(Path(f).read_text()).get("entries") or []
        except (OSError, json.JSONDecodeError, AttributeError) as e:
            print(f"  [warn] cannot read {f}: {e}")
            continue
        for e in entries:
            rid = normalize_remote_id(str(e.get("sessionId") or ""))
            if not rid:
                continue
            info = _entry(rid)
            info["tags"].add(TAG_SPACES)
            info["folders"] = [p for p in (e.get("folders") or []) if isinstance(p, str)]
            info["memory_enabled"] = e.get("memoryEnabled")

    rcw_seen: dict = {}
    digest_seen: dict = {}
    logs_dir = Path(logs_dir) if logs_dir else CLAUDE_DESKTOP_LOGS_DIR
    log_files = sorted({p for g in _LOG_GLOBS for p in logs_dir.glob(g)}) if logs_dir.is_dir() else []
    for f in log_files:
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if "_01" not in line and "rcw-" not in line and "sessionIdDigest" not in line:
                        continue
                    ts = _log_ts_utc(line)
                    tag = _line_tag(line)
                    for m in _RCW_RE.finditer(line):
                        rcw_seen.setdefault(m.group(1), []).append(ts)
                    for m in _DIGEST_RE.finditer(line):
                        digest_seen.setdefault(m.group(1), []).append(ts)
                    for m in _CLOUD_ID_RE.finditer(line):
                        info = _entry(f"session_{m.group(1)}")
                        info["tags"].add(tag)
                        _touch(info, ts)
        except OSError as e:
            print(f"  [warn] cannot read {f}: {e}")

    by_lower = {rid[len("session_"):].lower(): rid for rid in found if rid.startswith("session_")}
    for suffix, stamps in rcw_seen.items():
        rid = by_lower.get(suffix)
        if not rid:
            continue  # case lost: unresolvable until the id shows up in its real form
        info = found[rid]
        info["tags"].add("vm")
        for ts in stamps:
            _touch(info, ts)

    # The only trace of a session resumed through the remote-devices tools.
    by_digest = {session_digest(rid): rid for rid in found if rid.startswith("session_")}
    unknown_active: dict = {}
    for digest, stamps in digest_seen.items():
        stamps = [t for t in stamps if t]
        rid = by_digest.get(digest)
        if rid:
            found[rid]["tags"].add("web-cowork")
            for ts in stamps:
                _touch(found[rid], ts)
        elif stamps:
            unknown_active[digest] = (min(stamps), max(stamps))

    # Desktop's own record of remote control: a LOCAL Claude Code session lists the
    # cloud ids that mirror it. Durable, unlike the rotating logs.
    for d in (desktop_session_dirs if desktop_session_dirs is not None else COWORK_SESSIONS_DIRS_DEFAULT):
        for f in Path(d).glob("*/*/local_*.json"):
            try:
                bridge_ids = json.loads(f.read_text()).get("bridgeSessionIds") or []
            except (OSError, json.JSONDecodeError, AttributeError):
                continue
            for raw in bridge_ids:
                rid = normalize_remote_id(str(raw))
                if rid in found:
                    found[rid]["tags"].add(TAG_BRIDGE)

    for info in found.values():
        tags = info["tags"]
        if tags & _COWORK_TAGS:
            info["family"] = "cowork"
        elif "dispatch" in tags:
            info["family"] = "dispatch"
        else:
            info["family"] = "code_cloud"
        if tags & {TAG_REMOTE_CONTROL, TAG_BRIDGE}:
            info["excluded"] = "remote control of a local session (already in LAV as claude_code)"
        elif not info["remote_id"].startswith("session_"):
            info["excluded"] = "not a cloud session id (session_/cse_): fetch only with --id"
        else:
            info["excluded"] = ""
    return found, unknown_active


# ===========================================================================
# STATUS (derived from the filesystem, no registry table)
# ===========================================================================

def _paths(remote_id: str, sessions_dir: Path) -> dict:
    return {
        "jsonl": sessions_dir / f"{remote_id}.jsonl",
        "meta": sessions_dir / f"{remote_id}.meta.json",
        "failed": sessions_dir / f"{remote_id}.failed.json",
        "subagents": sessions_dir / remote_id,
    }


def _read_json(path: Path) -> Optional[dict]:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _write_json_atomic(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=sorted))
    os.replace(tmp, path)


def session_status(remote_id: str, sessions_dir: Path, info: Optional[dict] = None) -> str:
    """pending | fetched | stale | failed | failed-permanent.

    `stale` = fetched, but Desktop logged activity on the session after our last
    teleport attempt: the cloud transcript has probably grown, re-fetch it.
    """
    p = _paths(remote_id, sessions_dir)
    if p["jsonl"].exists() and p["meta"].exists():
        meta = _read_json(p["meta"]) or {}
        last_attempt = meta.get("last_attempt_at") or meta.get("fetched_at") or ""
        if info and info.get("last_seen") and info["last_seen"] > last_attempt:
            return "stale"
        return "fetched"
    failed = _read_json(p["failed"])
    if failed is not None:
        return "failed-permanent" if failed.get("permanent") else "failed"
    return "pending"


# ===========================================================================
# FETCH (`claude --teleport`)
# ===========================================================================

# Inherited from a parent Claude Code (nested-session markers, sockets) or from
# LAV's own .env. An API key would also take over from the claude.ai login that
# teleport needs, and bill any accidental model call to the API account.
_ENV_SCRUB_KEYS = {"CLAUDECODE", "CLAUDE_PID"}
_ENV_SCRUB_PREFIXES = ("CLAUDE_CODE_", "CLAUDE_PLUGIN", "CLAUDE_AGENT_SDK", "CLAUDE_EFFORT", "ANTHROPIC_")


def build_subprocess_env() -> dict:
    env = {k: v for k, v in os.environ.items()
           if k not in _ENV_SCRUB_KEYS and not k.startswith(_ENV_SCRUB_PREFIXES)}
    local_bin = str(Path.home() / ".local" / "bin")  # absent from the LaunchAgent PATH
    path = env.get("PATH", "")
    if local_bin not in path.split(os.pathsep):
        env["PATH"] = os.pathsep.join(filter(None, (local_bin, path)))
    return env


def _claude_projects_root(env: dict) -> Path:
    base = env.get("CLAUDE_CONFIG_DIR")
    return (Path(base).expanduser() if base else Path.home() / ".claude") / "projects"


def _teleport_slug_dirs(projects_root: Path) -> list:
    if not projects_root.is_dir():
        return []
    return [d for d in projects_root.iterdir() if d.is_dir() and is_lav_internal_project_dir(d)]


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _quarantine(paths: list, dest_dir: Path, prefix: str) -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    for p in paths:
        shutil.move(str(p), str(dest_dir / f"{prefix}.{p.name}"))


def _clear_slug_dirs(projects_root: Path, orphans_dir: Path) -> None:
    """Nothing LAV teleported may stay under ~/.claude/projects."""
    for d in _teleport_slug_dirs(projects_root):
        leftovers = list(d.iterdir())
        if leftovers:
            _quarantine(leftovers, orphans_dir, time.strftime("%Y%m%dT%H%M%S"))
        try:
            d.rmdir()
        except OSError:
            pass


def _kill_group(proc: subprocess.Popen) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            return
        try:
            proc.wait(timeout=10)
            return
        except subprocess.TimeoutExpired:
            continue


def inspect_transcript(path: Path) -> dict:
    """What a teleported file contains, split into cloud lines and local ones."""
    out = {"remote_lines": 0, "local_assistant_lines": 0, "teleport_cost_usd": None,
           "teleport_entrypoint": "", "remote_client_version": "", "remote_first_ts": None,
           "remote_last_ts": None}
    for rec in parse_jsonl_file(path):
        rtype = rec.get("type")
        if rtype == "cost-state":
            out["teleport_cost_usd"] = rec.get("totalCostUSD")
            continue
        if rtype not in ("user", "assistant"):
            continue
        if rec.get("remoteSourced") is True:
            out["remote_lines"] += 1
            # Not a fact about the cloud session: teleport rewrites it with ITS OWN
            # entrypoint (cli from a terminal, claude-vscode from the extension,
            # sdk-cli with --output-format json). Diagnostic only, never stored.
            out["teleport_entrypoint"] = out["teleport_entrypoint"] or str(rec.get("entrypoint") or "")
            out["remote_client_version"] = out["remote_client_version"] or str(rec.get("version") or "")
            ts = rec.get("timestamp")
            if ts:
                out["remote_first_ts"] = min(filter(None, (out["remote_first_ts"], ts)))
                out["remote_last_ts"] = max(filter(None, (out["remote_last_ts"], ts)))
        elif rtype == "assistant":
            out["local_assistant_lines"] += 1
    return out


# Tool-input keys that hold a path. Cowork remote reaches the Mac through the
# remote-devices tools (device_list_dir: path, device_bash: command), not Read/Bash.
_PATH_KEYS = ("file_path", "path", "notebook_path", "directory", "cwd")


def resolve_project(folders: list, transcript: Optional[Path]) -> str:
    """The cowork_desktop rule: the first real project name from the granted
    folders, else from the paths in the cloud tool calls (first hit in file
    order), else cowork_default. See jsonl.infer_cowork_project."""
    for folder in folders:
        project = infer_cowork_project(folder)
        if project:
            return project
    if transcript is not None:
        for rec in parse_jsonl_file(transcript):
            if rec.get("remoteSourced") is not True or rec.get("type") != "assistant":
                continue
            for call in extract_tool_calls(rec):
                tool_input = call.get("input") if isinstance(call.get("input"), dict) else {}
                candidates = [tool_input.get(k) for k in _PATH_KEYS]
                command = tool_input.get("command")
                if isinstance(command, str):
                    candidates += command.split()
                for candidate in candidates:
                    if isinstance(candidate, str):
                        project = infer_cowork_project(candidate.strip("'\""))
                        if project:
                            return project
    return DEFAULT_PROJECT


def _remote_uuids(path: Path) -> set:
    return {rec.get("uuid") for rec in parse_jsonl_file(path)
            if rec.get("remoteSourced") is True and rec.get("uuid")}


def fetch_one(remote_id: str, info: dict, *, sessions_dir: Path, claude_bin: str,
              claude_version: str, env: dict, timeout: int) -> bool:
    """Teleport one session and file its transcript under sessions_dir."""
    p = _paths(remote_id, sessions_dir)
    projects_root = _claude_projects_root(env)
    slug = re.sub(r"[^A-Za-z0-9-]", "-", str(COWORK_REMOTE_TELEPORT_CWD))
    if not is_lav_internal_project_dir(projects_root / slug):
        raise RuntimeError(f"teleport slug {slug!r} would not be skipped by the claude_code parser")
    COWORK_REMOTE_TELEPORT_CWD.mkdir(parents=True, exist_ok=True)
    if (COWORK_REMOTE_TELEPORT_CWD / ".git").exists():
        raise RuntimeError(f"{COWORK_REMOTE_TELEPORT_CWD} must not be a git checkout")
    _clear_slug_dirs(projects_root, sessions_dir.parent / "orphans")

    cmd = [claude_bin, "--teleport", remote_id, "-p", "/cost",
           "--output-format", "json", "--strict-mcp-config", "--no-chrome"]
    started = time.time()
    timed_out = False
    proc = subprocess.Popen(cmd, cwd=str(COWORK_REMOTE_TELEPORT_CWD), env=env,
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_group(proc)
        stdout, stderr = proc.communicate()
    duration_s = round(time.time() - started, 1)

    local_uuid = ""
    for line in reversed((stdout or "").strip().splitlines()):
        try:
            local_uuid = str(json.loads(line).get("session_id") or "")
            break
        except (json.JSONDecodeError, AttributeError):
            continue

    produced = [f for d in _teleport_slug_dirs(projects_root) for f in d.glob("*.jsonl")]
    transcript = next((f for f in produced if f.stem == local_uuid), None)
    if transcript is None and len(produced) == 1:
        transcript = produced[0]
        local_uuid = local_uuid or transcript.stem
    facts = inspect_transcript(transcript) if transcript else {}

    # Re-fetch: the stored file is replaced only by a SUPERSET of itself. Cloud
    # lines keep their uuid across teleports (verified), so a re-parse inserts
    # just the new turns; a file missing old lines would mean the cloud copy
    # shrank or changed shape, and the one we hold is kept.
    old_meta = _read_json(p["meta"]) if p["jsonl"].exists() else None
    missing = 0
    if old_meta is not None and transcript is not None and facts.get("remote_lines"):
        missing = len(_remote_uuids(p["jsonl"]) - _remote_uuids(transcript))

    error = ""
    if timed_out:
        error = f"timeout after {timeout}s"
    elif proc.returncode != 0:
        error = f"exit code {proc.returncode}"
    elif transcript is None:
        error = f"expected one transcript, found {len(produced)}"
    elif not facts.get("remote_lines"):
        error = "transcript has no remoteSourced lines"
    elif missing:
        error = f"re-fetch lacks {missing} cloud lines of the stored transcript: kept the stored one"

    now = _now_iso()
    if error:
        if transcript is not None:
            _quarantine([transcript], sessions_dir.parent / "failed", remote_id)
        _clear_slug_dirs(projects_root, sessions_dir.parent / "orphans")
        if old_meta is not None:
            # The session stays fetched; recording the attempt stops it from being
            # retried every run until Desktop logs new activity on it.
            _write_json_atomic(p["meta"], {**old_meta, "last_attempt_at": now,
                                           "last_refetch_error": error})
        else:
            prev = _read_json(p["failed"]) or {}
            _write_json_atomic(p["failed"], {
                "remote_id": remote_id,
                "error": error,
                "attempts": int(prev.get("attempts") or 0) + 1,
                "last_attempt_at": now,
                "exit_code": proc.returncode,
                # Repo-bound Code sessions can only be resumed from a checkout of their repo.
                "permanent": "from a checkout of" in (stderr or ""),
                "teleport_claude_version": claude_version,
                "stdout_tail": (stdout or "")[-STDIO_TAIL:],
                "stderr_tail": (stderr or "")[-STDIO_TAIL:],
            })
        print(f"  FAILED {remote_id}: {error}")
        return False

    # Sidecar first: the parser only reads a .jsonl that already has its .meta.json.
    # Folders and project are frozen at the first fetch: a new project would give
    # the session a second interactions row (and a re-fetch can add tool paths).
    session_dir = transcript.parent / transcript.stem
    subagent_files = len(list(session_dir.rglob("agent-*.jsonl"))) if session_dir.is_dir() else 0
    old_meta = old_meta or {}
    folders = old_meta["folders"] if "folders" in old_meta else (info.get("folders") or [])
    _write_json_atomic(p["meta"], {
        "remote_id": remote_id,
        "source": SOURCE_COWORK_REMOTE,
        "family": old_meta.get("family") or info.get("family", ""),
        "tags": sorted(info.get("tags") or old_meta.get("tags") or []),
        "folders": folders,
        "project": old_meta.get("project") or resolve_project(folders, transcript),
        "memory_enabled": info.get("memory_enabled"),
        "first_seen": info.get("first_seen") or old_meta.get("first_seen"),
        "last_seen": info.get("last_seen") or old_meta.get("last_seen"),
        "first_fetched_at": old_meta.get("first_fetched_at") or old_meta.get("fetched_at") or now,
        "fetched_at": now,
        "last_attempt_at": now,
        "fetch_count": int(old_meta.get("fetch_count") or (1 if old_meta else 0)) + 1,
        "fetched_on_host": _canonical_hostname(),
        "fetched_on_os": platform.system().lower(),
        "fetched_home": str(Path.home()),
        "fetched_by": old_meta.get("fetched_by") or getpass.getuser(),
        "teleport_claude_version": claude_version,
        "local_session_uuid": local_uuid,
        "exit_code": proc.returncode,
        "duration_s": duration_s,
        "subagent_files": subagent_files,
        **facts,
        "stdout_tail": (stdout or "")[-STDIO_TAIL:],
        "stderr_tail": (stderr or "")[-STDIO_TAIL:],
    })
    previous_lines = old_meta.get("remote_lines")
    shutil.move(str(transcript), str(p["jsonl"]))
    if session_dir.is_dir():
        if p["subagents"].exists():
            shutil.rmtree(p["subagents"])
        shutil.move(str(session_dir), str(p["subagents"]))
    _clear_slug_dirs(projects_root, sessions_dir.parent / "orphans")
    p["failed"].unlink(missing_ok=True)

    grown = "" if previous_lines is None else f" (re-fetch, was {previous_lines})"
    print(f"  OK {remote_id}: {facts['remote_lines']} cloud lines{grown}, {duration_s}s")
    if facts.get("local_assistant_lines") or facts.get("teleport_cost_usd"):
        print(f"  [warn] the teleport itself called the model: "
              f"{facts['local_assistant_lines']} local assistant lines, "
              f"${facts['teleport_cost_usd']} (not counted)")
    return True


def _resolve_claude_bin(explicit: Optional[str], env: dict) -> Optional[str]:
    if explicit:
        return explicit
    found = shutil.which("claude", path=env.get("PATH"))
    if found:
        return found
    fallback = Path.home() / ".local" / "bin" / "claude"
    return str(fallback) if fallback.exists() else None


def select_for_fetch(discovered: dict, sessions_dir: Path, *, explicit_ids: list,
                     max_fetch: int, min_idle_minutes: int, retry_failed: bool,
                     refetch_all: bool = False) -> list:
    """Ids to teleport this run, in order: never-fetched sessions first, then
    stale ones (re-fetch); with `refetch_all`, every fetched one last (manual
    brute force). Explicit ids replace the automatic selection and bypass
    its exclusions and idle check, and re-fetch an already fetched session: they
    are the owner's call."""
    chosen = []
    for raw in explicit_ids:
        rid = normalize_remote_id(raw)
        info = discovered.get(rid) or {"remote_id": rid, "tags": {"manual"}, "family": "",
                                       "folders": [], "excluded": ""}
        if info.get("excluded"):
            print(f"  [note] {rid} is excluded by discovery ({info['excluded']}): fetching anyway (--id)")
        status = session_status(rid, sessions_dir)
        if status == "failed-permanent" or (status == "failed" and not retry_failed):
            print(f"  [skip] {rid} is {status} (use --retry-failed for a non-permanent failure)")
        else:
            chosen.append((rid, info))
    if explicit_ids:
        return chosen[:max_fetch] if max_fetch >= 0 else chosen

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=min_idle_minutes)
    cutoff_iso = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
    rank = {"pending": 0, "stale": 1, "failed": 2, "fetched": 3}
    candidates = []
    for info in discovered.values():
        if info["excluded"]:
            continue
        status = session_status(info["remote_id"], sessions_dir, info)
        if (status in ("pending", "stale") or (status == "failed" and retry_failed)
                or (status == "fetched" and refetch_all)):
            candidates.append((rank[status], info["first_seen"] or "9999", info))
    for _, _, info in sorted(candidates, key=lambda c: c[:2]):
        if info["last_seen"] and info["last_seen"] > cutoff_iso:
            print(f"  [skip] {info['remote_id']} active in the last {min_idle_minutes} min")
            continue
        chosen.append((info["remote_id"], info))
    return chosen[:max_fetch] if max_fetch >= 0 else chosen


def run_fetch(discovered: dict, sessions_dir: Path, args) -> tuple:
    """Returns (fetched, failed) counts, or None when another fetch holds the lock."""
    import fcntl  # Unix only; imported here so the server still imports on Windows

    COWORK_REMOTE_DIR.mkdir(parents=True, exist_ok=True)
    sessions_dir.mkdir(parents=True, exist_ok=True)
    lock_fh = open(COWORK_REMOTE_DIR / "fetch.lock", "w")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_fh.close()
        return None
    try:
        chosen = select_for_fetch(discovered, sessions_dir, explicit_ids=args.id,
                                  max_fetch=args.max_fetch, min_idle_minutes=args.min_idle_minutes,
                                  retry_failed=args.retry_failed, refetch_all=args.refetch_all)
        print(f"\nTeleport: {len(chosen)} session(s) to fetch")
        for rid, info in chosen:
            print(f"  - {rid} [{info.get('family', '')}] {session_status(rid, sessions_dir, info)}")
        if args.dry_run or not chosen:
            return (0, 0)
        env = build_subprocess_env()
        claude_bin = _resolve_claude_bin(args.claude_bin, env)
        if not claude_bin:
            print("  [error] claude CLI not found (use --claude-bin)")
            return (0, len(chosen))
        try:
            claude_version = subprocess.run([claude_bin, "--version"], env=env, capture_output=True,
                                            text=True, timeout=60).stdout.strip()
        except (OSError, subprocess.SubprocessError) as e:
            print(f"  [error] cannot run {claude_bin}: {e}")
            return (0, len(chosen))
        ok = ko = 0
        for rid, info in chosen:
            if fetch_one(rid, info, sessions_dir=sessions_dir, claude_bin=claude_bin,
                         claude_version=claude_version, env=env, timeout=args.timeout):
                ok += 1
            else:
                ko += 1
        return (ok, ko)
    finally:
        fcntl.flock(lock_fh, fcntl.LOCK_UN)
        lock_fh.close()


# ===========================================================================
# PARSE
# ===========================================================================

def parse_cowork_remote_sessions(conn, sessions_dir: Optional[Path] = None,
                                 user: Optional[str] = None, host: Optional[str] = None) -> list:
    """Parse every fetched transcript into the unified DB as source cowork_remote.

    No watermark: the whole folder is re-read every run (tens of files) and every
    insert is duplicate-guarded, so a re-run changes nothing. Host and user come
    from the sidecar, so the same files give the same rows on every node.
    """
    sessions_dir = Path(sessions_dir) if sessions_dir else COWORK_REMOTE_SESSIONS_DIR
    stats = {"source": SOURCE_COWORK_REMOTE, "sessions": 0, "messages_processed": 0,
             "tool_calls_processed": 0, "local_lines_skipped": 0, "incomplete": 0,
             "subagent_files_not_parsed": 0}
    if not sessions_dir.is_dir():
        print(f"cowork_remote sessions directory not found: {sessions_dir}")
        return [stats]

    files = sorted(sessions_dir.glob("*.jsonl"))
    print(f"\nParsing cowork_remote sessions from: {sessions_dir}")
    print(f"  Found {len(files)} transcripts")
    dropped_before = dropped_tool_results()

    for path in files:
        meta = _read_json(_paths(path.stem, sessions_dir)["meta"])
        if not meta:
            print(f"  [warn] {path.name}: no .meta.json (incomplete fetch), skipped")
            stats["incomplete"] += 1
            continue

        sid = meta.get("remote_id") or path.stem
        folders = [f for f in (meta.get("folders") or []) if isinstance(f, str)]
        project_name = meta.get("project") or resolve_project(folders, path)
        project_id = get_or_create_project(conn, project_name)
        user_id = get_or_create_user(conn, user or meta.get("fetched_by") or getpass.getuser())
        host_id = get_or_create_host(conn, host or meta.get("fetched_on_host") or _canonical_hostname(),
                                     meta.get("fetched_on_os") or "", meta.get("fetched_home") or "")

        upsert_session_source(
            conn, sid, project_id, SOURCE_COWORK_REMOTE,
            client_version=str(meta.get("remote_client_version") or ""),
            meta={k: meta.get(k) for k in (
                "remote_id", "family", "tags", "folders", "fetched_at", "fetched_on_host",
                "local_session_uuid", "teleport_claude_version")},
        )

        cwd = folders[0] if folders else ""
        seen_tool_use_ids: set = set()
        pending_tool_results: dict = {}
        for rec in parse_jsonl_file(path):
            if rec.get("type") not in ("user", "assistant"):
                continue
            if rec.get("remoteSourced") is not True:
                stats["local_lines_skipped"] += 1
                continue
            msg = {**rec, "sessionId": sid, "cwd": cwd, "gitBranch": ""}
            stats["messages_processed"] += 1

            process_message_content(msg, conn, project_id, user_id, host_id)
            process_tool_results(msg, conn, project_id, seen_tool_use_ids, pending_tool_results)
            for tool_call in extract_tool_calls(msg):
                process_tool_call(tool_call, msg, conn, project_id, user_id, host_id)
                if tool_call.get("id"):
                    seen_tool_use_ids.add(tool_call["id"])
                stats["tool_calls_processed"] += 1
            process_token_usage(msg, conn, project_id, user_id, host_id)

        flush_pending_tool_results(conn, pending_tool_results)
        update_interaction(sid, project_name, conn, project_id, user_id, host_id,
                           parent_session_id=None, agent_id=None, workflow_id="")
        conn.commit()
        stats["sessions"] += 1

        sub_dir = _paths(sid, sessions_dir)["subagents"]
        if sub_dir.is_dir():
            n = len(list(sub_dir.rglob("agent-*.jsonl")))
            if n:
                stats["subagent_files_not_parsed"] += n
                print(f"  [warn] {sid}: {n} subagent transcripts NOT parsed (not supported yet)")

    dropped = dropped_tool_results() - dropped_before
    stats["tool_results_dropped"] = dropped
    if dropped:
        print(f"  {dropped} tool_results DROPPED (pending buffer full)")
    print(f"  Sessions: {stats['sessions']}  messages: {stats['messages_processed']}  "
          f"tool calls: {stats['tool_calls_processed']}  "
          f"local lines skipped: {stats['local_lines_skipped']}")
    return [stats]


# ===========================================================================
# CLI
# ===========================================================================

def _print_list(discovered: dict, unknown_active: dict, sessions_dir: Path) -> None:
    rows = sorted(discovered.values(), key=lambda i: (i["first_seen"] or "9999", i["remote_id"]))
    counts: dict = {}
    print(f"{'remote_id':34} {'family':10} {'status':16} {'first_seen':10} {'last_seen':10}  tags / note")
    for i in rows:
        status = "excluded" if i["excluded"] else session_status(i["remote_id"], sessions_dir, i)
        counts[status] = counts.get(status, 0) + 1
        note = i["excluded"] or ", ".join(i["folders"])
        print(f"{i['remote_id']:34} {i['family']:10} {status:16} {(i['first_seen'] or '')[:10]:10} "
              f"{(i['last_seen'] or '')[:10]:10}  {','.join(sorted(i['tags']))}  {note}")
    print("\n" + "  ".join(f"{k}: {v}" for k, v in sorted(counts.items())) + f"  (total {len(rows)})")
    if unknown_active:
        print(f"\n{len(unknown_active)} session(s) Desktop logged activity on whose id never appeared "
              f"in clear (cannot be fetched until it does):")
        for digest, (first, last) in sorted(unknown_active.items(), key=lambda kv: kv[1][1], reverse=True):
            print(f"  sha256_{digest[:12]}…  first {first[:16]}  last {last[:16]}")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Fetch (claude --teleport) and parse Claude cloud sessions — Cowork "
                    "remote and Claude Code cloud — as source cowork_remote (LAV-94)."
    )
    ap.add_argument("--list", action="store_true",
                    help="List discovered cloud session ids with family, status and tags, then exit")
    ap.add_argument("--fetch", action="store_true",
                    help="Teleport pending sessions, then stale ones (activity logged after "
                         "their last fetch), before parsing")
    ap.add_argument("--scheduled", action="store_true",
                    help='Fetch only if config.json has "cowork_remote_fetch": true (lav-parser.sh)')
    ap.add_argument("--id", action="append", default=[],
                    help="Fetch this session (session_…, cse_… or claude.ai URL), or re-fetch it "
                         "if already fetched. Repeatable. Bypasses the discovery exclusions "
                         "and the idle check")
    ap.add_argument("--max-fetch", type=int, default=3,
                    help="Max teleports per run (default 3, -1 = no cap)")
    ap.add_argument("--min-idle-minutes", type=int, default=30,
                    help="Skip sessions seen in the Desktop logs more recently than this (default 30)")
    ap.add_argument("--retry-failed", action="store_true",
                    help="Retry non-permanent failures")
    ap.add_argument("--refetch-all", action="store_true",
                    help="Brute force, manual only: re-fetch EVERY already fetched session, "
                         "not just the stale ones (still capped by --max-fetch, -1 = no cap)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print what would be fetched; no teleport, no parse")
    ap.add_argument("--sessions-dir", default=None,
                    help=f"Transcripts folder (default {COWORK_REMOTE_SESSIONS_DIR})")
    ap.add_argument("--spaces-file", action="append", default=None,
                    help="remote-session-spaces.json to read (default: auto-discovered)")
    ap.add_argument("--logs-dir", default=None,
                    help=f"Claude Desktop logs folder (default {CLAUDE_DESKTOP_LOGS_DIR})")
    ap.add_argument("--claude-bin", default=None, help="Path to the claude CLI")
    ap.add_argument("--timeout", type=int, default=300, help="Seconds per teleport (default 300)")
    ap.add_argument("--user", default=None, help="Override the user (default: from the sidecar)")
    ap.add_argument("--host", default=None, help="Override the host (default: from the sidecar)")
    args = ap.parse_args()

    sessions_dir = Path(args.sessions_dir).expanduser() if args.sessions_dir else COWORK_REMOTE_SESSIONS_DIR
    runtime_config = load_runtime_config()
    do_fetch = (args.fetch or args.refetch_all or bool(args.id)
                or (args.scheduled and runtime_config.get("cowork_remote_fetch")))

    if args.list or do_fetch:
        discovered, unknown_active = discover(
            spaces_files=args.spaces_file,
            logs_dir=Path(args.logs_dir).expanduser() if args.logs_dir else None)
        if args.list:
            _print_list(discovered, unknown_active, sessions_dir)
            return 0

    rc = 0
    fetched = 0
    if do_fetch:
        result = run_fetch(discovered, sessions_dir, args)
        if result is None:
            print("Another cowork_remote fetch is running: nothing done.")
            return 2
        fetched, failed = result
        rc = 1 if failed else 0
    if args.dry_run:
        return rc

    conn = init_db()
    try:
        parse_cowork_remote_sessions(conn, sessions_dir, user=args.user, host=args.host)
    finally:
        conn.close()

    # Old sessions sit behind the collector's export cursor and will not travel
    # over sync (copy sessions/ and parse on each node); recent ones will.
    if fetched:
        notify_collector(runtime_config)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
