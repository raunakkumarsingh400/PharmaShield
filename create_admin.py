"""
Create or update the ADMIN account (there is no default admin password).

    python create_admin.py
"""
from pharmashield.envfile import load_env

load_env()

import getpass
import os
import sys

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "pharmashield.settings")

from core.db import db  # noqa: E402


def main():
    email = input("Admin email: ").strip()
    password = os.getenv("PHARMASHIELD_ADMIN_PASSWORD") or getpass.getpass(
        "Admin password (min 8 chars): "
    )

    ok, message = db.create_admin(email, password)
    print(message)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
