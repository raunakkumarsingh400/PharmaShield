"""
Sends the password-reset email.

Providers (first one configured wins):

  1. Brevo HTTPS API   BREVO_API_KEY + EMAIL_FROM_ADDRESS
       Works on hosts that block SMTP ports (Render free plan blocks 25/465/587).
  2. SMTP              EMAIL_HOST_USER + EMAIL_HOST_PASSWORD  (e.g. Gmail app password)
       Works on your own computer and on paid hosts.
  3. Console           nothing configured: the email (with the link) is printed
       in the terminal that runs the server. NOTHING is delivered to an inbox.

Errors are logged (never silently ignored) so you can see why an email failed.
"""

import json
import logging
import os
import urllib.error
import urllib.request

logger = logging.getLogger("pharmashield.mail")

BREVO_URL = "https://api.brevo.com/v3/smtp/email"


def build_brevo_request(api_key, sender_email, sender_name, to_email, subject, body):
    payload = {
        "sender": {"name": sender_name, "email": sender_email},
        "to": [{"email": to_email}],
        "subject": subject,
        "textContent": body,
    }

    return urllib.request.Request(
        BREVO_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "accept": "application/json",
            "content-type": "application/json",
            "api-key": api_key,
        },
        method="POST",
    )


def send_via_brevo(api_key, sender_email, sender_name, to_email, subject, body,
                   opener=urllib.request.urlopen):
    request = build_brevo_request(
        api_key, sender_email, sender_name, to_email, subject, body
    )

    with opener(request, timeout=15) as response:
        status = getattr(response, "status", 200)

        if status not in (200, 201, 202):
            raise RuntimeError(f"Brevo returned HTTP {status}")


def provider():
    if os.getenv("BREVO_API_KEY"):
        return "brevo"

    if os.getenv("EMAIL_HOST_USER") and os.getenv("EMAIL_HOST_PASSWORD"):
        return "smtp"

    return "console"

def build_otp_email(name, otp, minutes=10):
    """Subject and body of the "your verification code" email."""
    subject = "Your PharmaShield verification code"
    body = (
        f"Hello {name or 'there'},\n\n"
        "Use this code to recover your PharmaShield ID:\n\n"
        f"    {otp}\n\n"
        f"The code is valid for {minutes} minutes and can be used once.\n"
        "Do not share it with anyone.\n\n"
        "If you did not ask for this, you can ignore this email."
    )

    return subject, body


def build_id_email(name, user_id):
    """Subject and body of the "here is your PharmaShield ID" email."""
    subject = "Your PharmaShield ID"
    body = (
        f"Hello {name or 'there'},\n\n"
        "You asked to recover your PharmaShield ID. It is:\n\n"
        f"    {user_id}\n\n"
        "Use it on the login page together with your password.\n"
        "If you forgot your password too, use \"Forgot Password\" on the login page.\n\n"
        "If you did not ask for this, you can ignore this email."
    )

    return subject, body

def describe_config():
    """One readable line saying why email is (not) configured."""
    from pathlib import Path

    env_path = Path(__file__).resolve().parent.parent / ".env"
    user = os.getenv("EMAIL_HOST_USER", "").strip()
    password = os.getenv("EMAIL_HOST_PASSWORD", "").replace(" ", "")

    parts = [
        f".env file: {'FOUND' if env_path.is_file() else 'NOT FOUND'} at {env_path}",
        f"EMAIL_HOST_USER: {'set (' + user + ')' if user else 'MISSING'}",
        f"EMAIL_HOST_PASSWORD: {'set (' + str(len(password)) + ' chars)' if password else 'MISSING'}",
    ]

    hints = []
    if not env_path.is_file():
        hints.append(
            "Create a file named exactly .env (not .env.txt) next to manage.py "
            "(see .env.example), then restart the server."
        )
    elif not user or not password:
        hints.append(
            "The .env file does not contain EMAIL_HOST_USER and EMAIL_HOST_PASSWORD "
            "(or the lines start with #). Fix them, save, then RESTART the server "
            "(Ctrl+C, then runserver again): changes to .env are not auto-reloaded."
        )

    return " | ".join(parts) + ("\n  -> " + " ".join(hints) if hints else "")


def send_email(to_email, subject, body):
    """Return True if the provider accepted the message, False otherwise."""
    kind = provider()

    try:
        if kind == "brevo":
            sender = os.getenv("EMAIL_FROM_ADDRESS", "Pharmashield11@gmail.com").strip()

            if not sender:
                raise RuntimeError(
                    "EMAIL_FROM_ADDRESS (a sender verified in Brevo) is not set."
                )

            send_via_brevo(
                os.environ["BREVO_API_KEY"].strip(),
                sender,
                os.getenv("EMAIL_FROM_NAME", "PharmaShield"),
                to_email,
                subject,
                body,
            )

        else:
            from django.conf import settings
            from django.core.mail import send_mail

            send_mail(
                subject,
                body,
                settings.DEFAULT_FROM_EMAIL,
                [to_email],
                fail_silently=False,
            )

            if kind == "console":
                logger.warning(
                    "Email is NOT configured: the reset email above was only "
                    "printed in this terminal, nothing was sent to an inbox.\n  %s\n"
                    "  Tip: run  python check_setup.py --send-test you@gmail.com",
                    describe_config(),
                )

        return True

    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        logger.error("Email via %s failed: HTTP %s %s", kind, exc.code, detail)

    except Exception:
        logger.exception("Email via %s failed", kind)

    return False
