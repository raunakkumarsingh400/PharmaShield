from pathlib import Path
import os

from django.core.exceptions import ImproperlyConfigured

from .envfile import load_env

load_env()  # reads the .env file in the project folder (if present)


BASE_DIR = Path(__file__).resolve().parent.parent


def env_list(name, default=""):
    return [item.strip() for item in os.getenv(name, default).split(",") if item.strip()]


# =========================
# SECURITY
# =========================

# DEBUG defaults to True for local development only.
# On a server set DEBUG=False and DJANGO_SECRET_KEY.
DEBUG = os.getenv("DEBUG", "True") == "True"

SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "")

if not SECRET_KEY:
    if DEBUG:
        SECRET_KEY = "pharmashield-local-dev-key-do-not-use-in-production"
    else:
        raise ImproperlyConfigured(
            "DJANGO_SECRET_KEY must be set when DEBUG is False."
        )

ALLOWED_HOSTS = ["127.0.0.1", "localhost", ".onrender.com"] + env_list(
    "ALLOWED_HOSTS"
)

# Render sets this automatically to your service's public host name.
if os.getenv("RENDER_EXTERNAL_HOSTNAME"):
    ALLOWED_HOSTS.append(os.environ["RENDER_EXTERNAL_HOSTNAME"])

CSRF_TRUSTED_ORIGINS = ["https://*.onrender.com"] + env_list("CSRF_TRUSTED_ORIGINS")

if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = 31536000
    SECURE_CONTENT_TYPE_NOSNIFF = True

# Login sessions are stored in MongoDB Atlas (core/mongo_sessions.py), so the web
# server keeps no data on its own disk.
SESSION_ENGINE = "core.mongo_sessions"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"

# Automatic logout
#   * IDLE: logged out after this many minutes without any activity.
#   * ABSOLUTE: logged out this many minutes after login, even if active.
# Both can be changed with environment variables.
IDLE_TIMEOUT_MINUTES = int(os.getenv("IDLE_TIMEOUT_MINUTES", "15"))
ABSOLUTE_TIMEOUT_MINUTES = int(os.getenv("ABSOLUTE_TIMEOUT_MINUTES", "480"))

SESSION_COOKIE_AGE = IDLE_TIMEOUT_MINUTES * 60  # sliding idle timeout
SESSION_SAVE_EVERY_REQUEST = True  # every request pushes the expiry forward
SESSION_EXPIRE_AT_BROWSER_CLOSE = True
X_FRAME_OPTIONS = "DENY"


# =========================
# EMAIL (password reset)
# =========================
# See core/mailer.py. Configure ONE of:
#   * Brevo API (works on Render free):  BREVO_API_KEY, EMAIL_FROM_ADDRESS
#   * SMTP / Gmail app password:         EMAIL_HOST_USER, EMAIL_HOST_PASSWORD
# With neither, the reset link is only printed in the server terminal.

EMAIL_HOST_USER = os.getenv("EMAIL_HOST_USER", "").strip()
# Gmail shows app passwords as "abcd efgh ijkl mnop": remove the spaces.
EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD", "").replace(" ", "")

EMAIL_BACKEND = os.getenv("EMAIL_BACKEND") or (
    "django.core.mail.backends.smtp.EmailBackend"
    if EMAIL_HOST_USER and EMAIL_HOST_PASSWORD
    else "django.core.mail.backends.console.EmailBackend"
)
EMAIL_HOST = os.getenv("EMAIL_HOST", "smtp.gmail.com")
EMAIL_PORT = int(os.getenv("EMAIL_PORT", "587"))
EMAIL_USE_TLS = os.getenv("EMAIL_USE_TLS", "True") == "True"
EMAIL_TIMEOUT = 15
DEFAULT_FROM_EMAIL = os.getenv("DEFAULT_FROM_EMAIL") or (
    f"PharmaShield <{EMAIL_HOST_USER}>"
    if EMAIL_HOST_USER
    else "PharmaShield <no-reply@pharmashield.local>"
)


# =========================
# URL / MIDDLEWARE / APPS
# =========================

ROOT_URLCONF = "pharmashield.urls"

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "core.middleware.SessionTimeoutMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

INSTALLED_APPS = [
    "django.contrib.staticfiles",
]


# =========================
# TEMPLATES
# =========================

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "core.context.session_timeout",
            ],
        },
    },
]


# =========================
# DATABASE
# =========================
# All application data AND sessions live in MongoDB Atlas. Django itself needs a
# DATABASES entry, so this is an unused in-memory SQLite database: nothing is
# written to disk and no migrations are required.

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}


# =========================
# STATIC FILES
# =========================

STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"
    },
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
