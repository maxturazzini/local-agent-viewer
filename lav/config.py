"""
Configuration for LocalAgentViewer - multi-user, multi-host AI agent analytics.
"""
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from lav import PROJECT_ROOT

# ===========================================================================
# DATABASE
# ===========================================================================

# Database: local per-machine, outside OneDrive
# Each machine has its own DB in ~/.local/share/local-agent-viewer/
# The collector pulls data from agents via /api/export
_LOCAL_DB_PATH = Path.home() / ".local" / "share" / "local-agent-viewer" / "local_agent_viewer.db"
_LEGACY_DB_PATH = PROJECT_ROOT / "data" / "local_agent_viewer.db"
UNIFIED_DB_PATH = _LOCAL_DB_PATH if _LOCAL_DB_PATH.exists() else _LEGACY_DB_PATH

# ===========================================================================
# SOURCE DIRECTORIES
# ===========================================================================

# Default source directory for Claude Code interactions (JSONL)
CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"

# Additional Claude Desktop location (NOT JSONL, but useful as a discoverable hint/source)
CLAUDE_DESKTOP_SUPPORT_DIR = Path.home() / "Library" / "Application Support" / "Claude"

# Source directory for Codex sessions
CODEX_SESSIONS_DIR = Path.home() / ".codex" / "sessions"

# Cowork / Claude Desktop sessions (JSONL audit logs)
# Pre-v1.1.4498: "local-agent-mode-sessions", post-v1.1.4498: "claude-code-sessions"
COWORK_SESSIONS_DIRS_DEFAULT = [
    CLAUDE_DESKTOP_SUPPORT_DIR / "local-agent-mode-sessions",
    CLAUDE_DESKTOP_SUPPORT_DIR / "claude-code-sessions",
]

# ===========================================================================
# TOOL CLASSIFICATION
# ===========================================================================

# Tools to track for file operations (native Claude tools)
FILE_OPERATION_TOOLS = ["Read", "Write", "Edit"]

# Tools to track for search operations
SEARCH_TOOLS = ["Glob", "Grep"]

# Bash commands mapped to file operation categories
BASH_READ_COMMANDS = ['cat', 'head', 'tail', 'less', 'more']
BASH_WRITE_COMMANDS = ['cp', 'mv', 'touch']
BASH_DELETE_COMMANDS = ['rm', 'mkdir']

# All bash commands that interact with files (for general tracking)
FILE_COMMANDS = [
    'cat', 'head', 'tail', 'less', 'more',
    'cp', 'mv', 'rm', 'mkdir', 'touch',
    'ls', 'find', 'tree',
    'chmod', 'chown',
    'tar', 'zip', 'unzip',
    'sed', 'awk', 'sort', 'wc',
    'diff', 'patch',
]

# ===========================================================================
# QDRANT KNOWLEDGE BASE
# ===========================================================================

QDRANT_DATA_DIR = Path.home() / ".local" / "share" / "local-agent-viewer" / "qdrant_data"
QDRANT_COLLECTION = "interactions"

# Optional: HTTP URL to a remote Qdrant server (e.g. "http://your-server:6333").
# When set, all Qdrant operations use HTTP instead of the local file path above.
QDRANT_URL = os.getenv("QDRANT_URL", "")

EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_VECTOR_SIZE = 1536

TAGGING_MODEL = "claude-haiku-4-5-20251001"

# ===========================================================================
# CLASSIFICATION (configurable via .env)
# ===========================================================================

CLASSIFY_MODEL = os.getenv("LAV_CLASSIFY_MODEL", "gpt-4.1-mini")
CLASSIFY_BASE_URL = os.getenv("LAV_CLASSIFY_BASE_URL", "")

# System prompt: empty = built-in default; file path = read from file; otherwise inline text
_classify_prompt_val = os.getenv("LAV_CLASSIFY_SYSTEM_PROMPT", "")
if _classify_prompt_val and os.path.isfile(_classify_prompt_val):
    with open(_classify_prompt_val, encoding="utf-8") as _f:
        CLASSIFY_SYSTEM_PROMPT = _f.read()
else:
    CLASSIFY_SYSTEM_PROMPT = _classify_prompt_val

CLASSIFY_BACKEND = os.getenv("LAV_CLASSIFY_BACKEND", "auto")  # auto, openai, ollama
CLASSIFY_MAX_CHARS = int(os.getenv("LAV_CLASSIFY_MAX_CHARS", "12000"))
CLASSIFY_LANGUAGE = os.getenv("LAV_CLASSIFY_LANGUAGE", "en")

CLASSIFICATIONS = [
    "development",
    "meeting",
    "analysis",
    "brainstorm",
    "support",
    "learning",
    "marketing",
    "operations",
]

SENSITIVITIES = [
    "public",
    "internal",
    "confidential",
    "restricted",
]


def get_openai_key() -> Optional[str]:
    """Get OpenAI API key for embeddings."""
    return os.getenv("OPENAI_API_KEY")


def get_anthropic_key() -> Optional[str]:
    """Get Anthropic API key for auto-tagging."""
    return os.getenv("ANTHROPIC_API_KEY")

# ===========================================================================
# RUNTIME CONFIG (agent/collector architecture)
# ===========================================================================

LOCAL_DATA_DIR = Path.home() / ".local" / "share" / "local-agent-viewer"

LAV_CONFIG_PATH = LOCAL_DATA_DIR / "config.json"


def load_runtime_config() -> dict:
    """Load runtime config (role, agents). Default: both, no agents."""
    defaults = {"role": "both", "port": 8764, "agents": []}
    LOCAL_DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not LAV_CONFIG_PATH.exists():
        return defaults
    try:
        with open(LAV_CONFIG_PATH) as f:
            cfg = json.load(f)
        cfg.setdefault("role", "both")
        cfg.setdefault("port", 8764)
        cfg.setdefault("agents", [])
        if cfg["role"] not in ("agent", "collector", "both"):
            cfg["role"] = "both"
        return cfg
    except (json.JSONDecodeError, OSError):
        return defaults


# ===========================================================================
# SERVER
# ===========================================================================

PORT = 8764

# ===========================================================================
# SOURCE TYPES
# ===========================================================================

SOURCE_CLAUDE_CODE = "claude_code"
SOURCE_CODEX_CLI = "codex_cli"
SOURCE_COWORK_DESKTOP = "cowork_desktop"
SOURCE_CHATGPT = "chatgpt"

# ===========================================================================
# CHATGPT EXPORT
# ===========================================================================

_chatgpt_env = os.environ.get("CHATGPT_EXPORT_PATH")
CHATGPT_EXPORT_PATH = Path(_chatgpt_env) if _chatgpt_env else None

# ===========================================================================
# LOCAL SETTINGS (optional manifest)
# ===========================================================================

LOCAL_SETTINGS_PATH = PROJECT_ROOT / ".claude" / "settings.local.json"


def _expand_path(value: str) -> Path:
    expanded = os.path.expandvars(os.path.expanduser(value))
    return Path(expanded)


def load_local_settings(path: Path = LOCAL_SETTINGS_PATH) -> Dict[str, Any]:
    """Load optional local settings JSON."""
    try:
        if not path.exists():
            return {}
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _coerce_path_list(value: Any) -> List[Path]:
    if not value:
        return []
    if isinstance(value, str):
        return [_expand_path(value)]
    if isinstance(value, list):
        out: List[Path] = []
        for item in value:
            if isinstance(item, str) and item.strip():
                out.append(_expand_path(item))
        return out
    return []


def get_claude_projects_dirs(
    overrides: Optional[List[str]] = None,
    include_desktop_hint: bool = True,
) -> List[Path]:
    """Resolve Claude projects dirs (JSONL roots).

    Precedence:
    1) CLI overrides (if provided)
    2) settings.local.json sources.claude_projects_dirs
    3) default ~/.claude/projects
    """
    if overrides:
        dirs = [_expand_path(p) for p in overrides if p and str(p).strip()]
    else:
        settings = load_local_settings()
        dirs = _coerce_path_list(
            (settings.get("sources") or {}).get("claude_projects_dirs")
        )
        if not dirs:
            dirs = [CLAUDE_PROJECTS_DIR]

    existing = [d for d in dirs if d.exists() and d.is_dir()]

    if (
        include_desktop_hint
        and CLAUDE_DESKTOP_SUPPORT_DIR.exists()
        and CLAUDE_DESKTOP_SUPPORT_DIR.is_dir()
    ):
        if CLAUDE_DESKTOP_SUPPORT_DIR not in existing:
            existing.append(CLAUDE_DESKTOP_SUPPORT_DIR)

    return existing


def get_codex_sessions_dirs(
    overrides: Optional[List[str]] = None,
) -> List[Path]:
    """Resolve Codex sessions dirs (JSONL roots)."""
    if overrides:
        dirs = [_expand_path(p) for p in overrides if p and str(p).strip()]
    else:
        settings = load_local_settings()
        dirs = _coerce_path_list(
            (settings.get("sources") or {}).get("codex_sessions_dirs")
        )
        if not dirs:
            dirs = [CODEX_SESSIONS_DIR]

    return [d for d in dirs if d.exists() and d.is_dir()]


def get_chatgpt_export_path(override: Optional[str] = None) -> Optional[Path]:
    """Resolve ChatGPT export path.

    Precedence:
    1) CLI override
    2) settings.local.json sources.chatgpt_export_path
    3) default CHATGPT_EXPORT_PATH
    """
    if override:
        p = _expand_path(override)
        return p if p.exists() else None

    settings = load_local_settings()
    configured = (settings.get("sources") or {}).get("chatgpt_export_path")
    if configured:
        p = _expand_path(configured)
        return p if p.exists() else None

    if CHATGPT_EXPORT_PATH is None:
        return None
    return CHATGPT_EXPORT_PATH if CHATGPT_EXPORT_PATH.exists() else None


def get_cowork_sessions_dirs(
    overrides: Optional[List[str]] = None,
) -> List[Path]:
    """Resolve Cowork/Claude Desktop session dirs (audit JSONL roots)."""
    if overrides:
        dirs = [_expand_path(p) for p in overrides if p and str(p).strip()]
    else:
        settings = load_local_settings()
        dirs = _coerce_path_list(
            (settings.get("sources") or {}).get("cowork_sessions_dirs")
        )
        if not dirs:
            dirs = list(COWORK_SESSIONS_DIRS_DEFAULT)

    return [d for d in dirs if d.exists() and d.is_dir()]
