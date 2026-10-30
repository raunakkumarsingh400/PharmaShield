"""
Tiny .env loader (no extra package needed).

Reads KEY=VALUE lines from a file named .env in the project folder and puts
them in os.environ. Non-empty variables already set in the terminal win over the file.
"""
import os
from pathlib import Path


def load_env(path=None):
    path = Path(path) if path else Path(__file__).resolve().parent.parent / ".env"

    if not path.is_file():
        return False

    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()

        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()

        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]

        # a variable already set (non-empty) in the terminal wins over the file;
        # an empty one (e.g. $env:EMAIL_HOST_USER="") does not
        if key and not os.environ.get(key):
            os.environ[key] = value

    return True
