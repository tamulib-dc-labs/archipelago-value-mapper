"""Settings from the environment, with a .env.local / .env fallback."""

import os
from pathlib import Path

_loaded = False


def load_env_file(directory: Path | None = None) -> None:
    """
    Read KEY=value lines from .env.local, then .env, in `directory` (default:
    the working directory), once per process.

    Values already in the environment are left alone, so anything set in the
    shell wins over the file.
    """
    global _loaded
    if _loaded:
        return
    _loaded = True

    directory = directory or Path.cwd()
    for name in (".env.local", ".env"):
        path = directory / name
        if not path.is_file():
            continue
        try:
            lines = path.read_text(encoding="utf-8-sig").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            value = value.strip()
            if len(value) > 1 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            os.environ.setdefault(key.strip(), value)


def setting(name: str) -> str | None:
    """An environment variable, falling back to the .env files."""
    value = os.getenv(name)
    if value:
        return value
    load_env_file()
    return os.getenv(name)
