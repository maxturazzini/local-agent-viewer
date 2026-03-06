"""LocalAgentViewer - AI agent analytics."""
import os
from importlib.metadata import version, PackageNotFoundError
from pathlib import Path

try:
    __version__ = version("local-agent-viewer")
except PackageNotFoundError:
    __version__ = "0.0.0-dev"

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parent


def _load_env():
    """Load .env from project root if present."""
    env_file = PROJECT_ROOT / ".env"
    if env_file.exists():
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, value = line.split('=', 1)
                    os.environ.setdefault(key.strip(), value.strip())


_load_env()
