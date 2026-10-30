from django.conf import settings


def session_timeout(request):
    """Makes the idle timeout available to templates (used by base.html)."""
    return {"idle_timeout_seconds": settings.IDLE_TIMEOUT_MINUTES * 60}
