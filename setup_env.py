"""
Creates / updates the .env file for you (no Notepad, no wrong file name).

    python setup_env.py

It asks for your MongoDB Atlas string and your Gmail details, writes a correct
.env next to manage.py, and tells you the next command to run.
"""
import re
from pathlib import Path

BASE = Path(__file__).resolve().parent
ENV = BASE / ".env"
DEFAULT_SENDER = "Pharmashield11@gmail.com"


def read_env():
    values = {}

    if ENV.is_file():
        for line in ENV.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()

            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip("\"'")

    return values


def ask(label, current="", help_text=""):
    if help_text:
        print(f"\n{help_text}")

    shown = f" [Enter = keep: {current[:40]}{'...' if len(current) > 40 else ''}]" if current else " [Enter = skip]"
    answer = input(f"{label}{shown}: ").strip().strip("\"'")

    return answer or current


def check_uri(uri):
    if not uri:
        return True

    if "<" in uri or ">" in uri:
        print("  ! The string still contains < >. Replace <db_password> with your real")
        print("    password and remove the angle brackets, then run this script again.")
        return False

    if not re.match(r"^mongodb(\+srv)?://", uri):
        print("  ! It must start with  mongodb+srv://  (copy it from Atlas > Connect > Drivers).")
        return False

    if "@" not in uri:
        print("  ! The string has no username/password part (user:password@...).")
        return False

    return True


def main():
    print("PharmaShield .env setup")
    print("-----------------------")
    print(f"File that will be written: {ENV}")

    old = read_env()

    uri = ask(
        "MongoDB Atlas connection string",
        old.get("PHARMASHIELD_MONGO_URI", ""),
        "1) Atlas > Database > Connect > Drivers. Paste the string with your real password "
        "(no < > brackets).",
    )

    while not check_uri(uri):
        uri = ask("MongoDB Atlas connection string", "")

    current_sender = old.get("EMAIL_HOST_USER", "")

    if not current_sender.lower().endswith("@gmail.com"):
        current_sender = DEFAULT_SENDER      # ignore placeholders like no-reply@...

    gmail = ask(
        "Gmail address that sends the reset emails",
        current_sender,
        "2) Email (password reset). Use a Gmail address.",
    )

    password = old.get("EMAIL_HOST_PASSWORD", "")

    if gmail:
        password = ask(
            "Gmail APP password (16 letters)",
            password,
            "   Google Account > Security > turn on 2-Step Verification > App passwords.\n"
            "   (Your normal Gmail password will NOT work.)",
        ).replace(" ", "")

        if password and len(password) != 16:
            print(f"  ! That is {len(password)} characters; a Gmail app password has exactly 16.")
            if input("    Save it anyway? (y/N): ").strip().lower() != "y":
                password = ""

    values = dict(old)
    values.setdefault("PHARMASHIELD_DB", "pharmashield")

    for key, value in (
        ("PHARMASHIELD_MONGO_URI", uri),
        ("EMAIL_HOST_USER", gmail),
        ("EMAIL_HOST_PASSWORD", password),
    ):
        if value:
            values[key] = value
        else:
            values.pop(key, None)

    lines = ["# Created by setup_env.py - never share or upload this file.", ""]
    lines += [f"{k}={v}" for k, v in values.items()]
    ENV.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"\nSaved {ENV}")
    print("\nNext:")
    print("  1) python check_setup.py")

    if gmail and password:
        print(f"  2) python check_setup.py --send-test {gmail}")

    print("  3) Restart the server (Ctrl+C, then python manage.py runserver)")


if __name__ == "__main__":
    main()
