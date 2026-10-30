"""
Checks your .env, email and MongoDB Atlas settings and tells you what is wrong.

    python check_setup.py                      # checks .env + MongoDB
    python check_setup.py --send-test you@gmail.com    # also sends a test email
"""
import os
import sys
from pathlib import Path

from pharmashield.envfile import load_env

BASE = Path(__file__).resolve().parent
ok_count = 0


def say(ok, text):
    global ok_count
    ok_count += 0 if ok else 1
    print(("  [OK]   " if ok else "  [FIX]  ") + text)


def mask_uri(uri):
    if "@" in uri and "://" in uri:
        scheme, rest = uri.split("://", 1)
        creds, host = rest.rsplit("@", 1)
        user = creds.split(":", 1)[0]
        return f"{scheme}://{user}:*****@{host}"
    return uri


print("\n0) Python and Django")
say(sys.version_info >= (3, 12),
    f"Python {sys.version_info.major}.{sys.version_info.minor} "
    + ("(OK for Django 6)" if sys.version_info >= (3, 12) else
       "- Django 6 needs Python 3.12+, so Django 5.2 LTS is installed instead. "
       "For the latest Django install Python 3.13 and re-create the venv."))
try:
    import django
    print(f"         Django {django.get_version()} installed")
except ImportError:
    say(False, "Django is not installed: run  pip install -r requirements.txt")

print("\n1) .env file")
found = load_env()
say(found, f".env found in {BASE}" if found else
    f"No .env in {BASE}. It must be named exactly .env (not .env.txt) "
    "and sit next to manage.py.")

for wrong in (".env.txt", ".env.example.txt", "env", "env.txt"):
    if (BASE / wrong).exists() and not found:
        say(False, f"Found '{wrong}' - rename it to exactly  .env")

print("\n2) Email")
user = os.getenv("EMAIL_HOST_USER", "").strip()
pwd = os.getenv("EMAIL_HOST_PASSWORD", "").replace(" ", "")
brevo = os.getenv("BREVO_API_KEY", "").strip()

if brevo:
    say(bool(os.getenv("EMAIL_FROM_ADDRESS")), "Brevo key set"
        + ("" if os.getenv("EMAIL_FROM_ADDRESS") else " but EMAIL_FROM_ADDRESS is missing"))
else:
    say(bool(user), f"EMAIL_HOST_USER = {user}" if user else "EMAIL_HOST_USER is not set")
    say(len(pwd) == 16, f"EMAIL_HOST_PASSWORD has {len(pwd)} characters"
        + ("" if len(pwd) == 16 else " (a Gmail app password has exactly 16, no spaces counted)"))
    if user and pwd and user.lower().endswith("@gmail.com") is False:
        say(False, "EMAIL_HOST_USER is not a gmail.com address; set EMAIL_HOST too (SMTP server).")

print("\n3) MongoDB")
uri = os.getenv("PHARMASHIELD_MONGO_URI", "")

if not uri:
    say(False, "PHARMASHIELD_MONGO_URI is not set -> the app uses local MongoDB "
               "(127.0.0.1). Put your Atlas string in .env to use Atlas.")
else:
    say(True, "URI = " + mask_uri(uri))
    if "<" in uri or ">" in uri:
        say(False, "The URI still contains < > : replace <db_password> with the real password "
                   "(without the angle brackets).")

try:
    from pymongo import MongoClient
except ImportError:
    MongoClient = None
    say(False, "pymongo is not installed: run  pip install -r requirements.txt")

if MongoClient:
    target = uri or "mongodb://127.0.0.1:27017"
    try:
        client = MongoClient(target, serverSelectionTimeoutMS=8000)
        client.admin.command("ping")
        db = client[os.getenv("PHARMASHIELD_DB", "pharmashield")]
        say(True, f"Connected. medicines: {db.medicines.estimated_document_count()}, "
                  f"users: {db.users.estimated_document_count()}")
        print("       Collections in Atlas:")
        for name in ("users", "medicines", "user_medicines", "profiles", "scans",
                     "reports", "sessions", "password_resets"):
            print(f"         {name:16} {db[name].estimated_document_count()}")
        if db.medicines.estimated_document_count() == 0:
            say(False, "medicines is empty: run  python import_medicines.py demo_data/PharmaShield_Demo_Data.xlsx")
    except Exception as exc:
        text = str(exc)
        hint = ""
        if "Authentication failed" in text or "bad auth" in text:
            hint = " -> wrong username/password (special characters must be URL-encoded; simplest: use a password with only letters and numbers)."
        elif "timed out" in text.lower() or "ServerSelection" in text:
            hint = " -> Atlas > Network Access: add your current IP (or 0.0.0.0/0)."
        say(False, "Could not connect: " + text[:160] + hint)

if "--send-test" in sys.argv:
    print("\n4) Test email")
    to = sys.argv[sys.argv.index("--send-test") + 1]
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "pharmashield.settings")
    import django
    django.setup()
    from core.mailer import provider, send_email
    print(f"  provider: {provider()}")
    sent = send_email(to, "PharmaShield test email", "If you can read this, email works.")
    say(sent and provider() != "console",
        f"Test email sent to {to}. Check inbox and spam." if sent and provider() != "console"
        else "Not delivered (see the error above, or provider is 'console' = nothing configured).")

print("\nAll good." if ok_count == 0 else f"\n{ok_count} thing(s) to fix (lines marked [FIX]).")
