import time

from django.conf import settings
from django.shortcuts import redirect
from django.utils.cache import add_never_cache_headers


class SessionTimeoutMiddleware:
    """
    * Enforces the absolute session lifetime (ABSOLUTE_TIMEOUT_MINUTES).
      The idle timeout is handled by Django's sliding session expiry
      (SESSION_COOKIE_AGE + SESSION_SAVE_EVERY_REQUEST).
    * Stops browsers from caching pages, so the Back button cannot show a
      logged-in page after logout or timeout.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.session.get("user"):
            logged_in_at = request.session.get("login_at")
            limit = settings.ABSOLUTE_TIMEOUT_MINUTES * 60

            if not logged_in_at or time.time() - logged_in_at > limit:
                request.session.flush()
                return redirect("/?reason=expired")

        response = self.get_response(request)
        add_never_cache_headers(response)

        return response
