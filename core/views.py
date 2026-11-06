import logging
import time
from functools import wraps

from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse

from .db import db
from .adminstats import mask_email
from .mailer import build_id_email, build_otp_email, provider, send_email
from .pdfreport import build_safety_pdf
from .qrcode_svg import MAX_BYTES, make_svg

logger = logging.getLogger("pharmashield")


# =========================================================
# ACCESS CONTROL
# =========================================================


def login_required(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.session.get("user"):
            return redirect("login")

        return view(request, *args, **kwargs)

    return wrapper


def admin_required(view):
    @wraps(view)
    @login_required
    def wrapper(request, *args, **kwargs):
        if request.session["user"].get("role") != "admin":
            return redirect("dashboard")

        return view(request, *args, **kwargs)

    return wrapper


def email_of(request):
    return request.session["user"]["email"]


# =========================================================
# AUTH
# =========================================================


def login_view(request):
    if request.session.get("user"):
        return redirect("dashboard")

    message = ""
    registered_id = request.GET.get("registered_id", "")

    if registered_id:
        message = "Registration successful! Please login."

    reason = request.GET.get("reason", "")

    if reason == "timeout":
        message = "You were logged out because of inactivity. Please login again."
    elif reason == "expired":
        message = "Your session has expired. Please login again."

    if request.method == "POST":
        user, message = db.login(
            request.POST.get("user_id", ""),
            request.POST.get("password", ""),
        )

        if user:
            request.session.cycle_key()  # new session id after login
            request.session["login_at"] = int(time.time())
            request.session["user"] = {
                "user_id": user.get("user_id", ""),
                "name": user.get("name", ""),
                "email": user.get("email", ""),
                "role": user.get("role", "user"),
            }

            return redirect("dashboard")

    return render(
        request,
        "login.html",
        {"message": message, "registered_id": registered_id, "db_online": db.online},
    )


def register_view(request):
    message = ""
    success = False
    user_id = None

    if request.method == "POST":
        password = request.POST.get("password", "")

        if password != request.POST.get("confirm_password", ""):
            message = "Passwords do not match."
        else:
            success, message, user_id = db.add_user(
                request.POST.get("name", ""),
                request.POST.get("email", ""),
                request.POST.get("mobile", ""),
                password,
            )

    return render(
        request,
        "register.html",
        {"message": message, "success": success, "user_id": user_id},
    )


def forgot_password(request):
    message = ""
    sent = False
    dev_link = ""

    if request.method == "POST":
        token, user = db.create_reset_token(
            request.POST.get("user_id", ""),
            request.POST.get("email", ""),
        )

        if token:
            link = request.build_absolute_uri(reverse("reset_password", args=[token]))

            sent_ok = send_email(
                user["email"],
                "PharmaShield password reset",
                f"Hello {user.get('name', '')},\n\n"
                f"Use this link to reset your password "
                f"(valid for 30 minutes, single use):\n\n{link}\n\n"
                "If you did not request this, you can ignore this email.",
            )

            if not sent_ok:
                # The reason is in the server log; the user still gets the
                # same generic answer (no account probing).
                logger.error("Password-reset email could not be sent to %s", user["email"])

            # Local development only: no email provider configured, so show the
            # link here instead of only in the terminal. Never on a live site
            # (DEBUG is False there).
            if settings.DEBUG and provider() == "console":
                dev_link = link

        # Same answer whether or not the account exists (no account probing).
        sent = True
        message = (
            "If the PharmaShield ID and email match an account, "
            "a password-reset link has been sent. Please also check your spam folder."
        )

    return render(
        request,
        "forgot_password.html",
        {"message": message, "sent": sent, "dev_link": dev_link},
    )

def forgot_id(request):
    """Step 1: registered email -> a 6-digit OTP is emailed."""
    if request.method == "POST":
        email = request.POST.get("email", "").strip().lower()
        user = db.find_user_by_email(email) if email else None
        dev_otp = ""

        if user and db.recovery_allowed(email):
            otp = db.create_id_otp(email)
            subject, body = build_otp_email(user.get("name", ""), otp)

            if not send_email(user["email"], subject, body):
                logger.error("OTP email could not be sent to %s", user["email"])

            # Local development only (DEBUG on, no email provider): show the OTP.
            if settings.DEBUG and provider() == "console":
                dev_otp = otp

        if email:
            # Same next screen whether or not the email is registered.
            request.session["id_recovery_email"] = email
            request.session["id_dev_otp"] = dev_otp

            return redirect("forgot_id_verify")

    return render(request, "forgot_id.html", {"step": "email"})


def forgot_id_verify(request):
    """Step 2: enter the OTP. Step 3: the ID is shown here and emailed."""
    email = request.session.get("id_recovery_email", "")

    if not email:
        return redirect("forgot_id")

    message = ""

    if request.method == "POST":
        user = db.find_user_by_email(email)

        if user and db.verify_id_otp(email, request.POST.get("otp", "")):
            subject, body = build_id_email(user.get("name", ""), user.get("user_id", ""))

            if not send_email(user["email"], subject, body):
                logger.error("PharmaShield ID email could not be sent to %s", user["email"])

            request.session.pop("id_recovery_email", None)
            request.session.pop("id_dev_otp", None)

            return render(
                request,
                "forgot_id.html",
                {"step": "done", "found_id": user.get("user_id", ""), "found_name": user.get("name", "")},
            )

        message = "That code is wrong or has expired. Check the code, or request a new one."

    return render(
        request,
        "forgot_id.html",
        {
            "step": "otp",
            "message": message,
            "masked_email": mask_email(email),
            "dev_otp": request.session.get("id_dev_otp", ""),
        },
    )

def reset_password(request, token):
    if not db.reset_token_valid(token):
        return render(
            request,
            "reset_password.html",
            {"message": "This reset link is invalid or has expired.", "success": False, "invalid": True},
        )

    message = ""
    success = False

    if request.method == "POST":
        password = request.POST.get("password", "")

        if password != request.POST.get("confirm_password", ""):
            message = "Passwords do not match."
        else:
            success, message = db.reset_password_with_token(token, password)

    return render(
        request,
        "reset_password.html",
        {"message": message, "success": success},
    )


def logout_view(request):
    request.session.flush()

    # the browser-side idle timer sends ?reason=timeout
    if request.GET.get("reason") == "timeout":
        return redirect("/?reason=timeout")

    return redirect("login")


@login_required
def keepalive(request):
    """Called by the page's activity timer so an active user is not logged out."""
    return JsonResponse({"ok": True})


# =========================================================
# DASHBOARD
# =========================================================


@login_required
def dashboard(request):
    return render(
        request,
        "dashboard.html",
        {"user": request.session["user"], "stats": db.stats(email_of(request))},
    )


# =========================================================
# MY MEDICINES / SEARCH / SAFETY
# =========================================================


@login_required
def my_medicines_view(request):
    email = email_of(request)
    message = ""
    success = False

    if request.method == "POST":
        if request.POST.get("action", "add") == "add":
            success, message = db.add_user_medicine(
                email,
                product_id=request.POST.get("product_id", ""),
                medicine_name=request.POST.get("medicine_name", ""),
                dose=request.POST.get("dose", ""),
                frequency=request.POST.get("frequency", ""),
                duration=request.POST.get("duration", ""),
            )
        else:
            success, message = db.remove_user_medicine(
                email, request.POST.get("medicine_id", "")
            )

    safety = db.medication_safety_check(email)

    return render(
        request,
        "my_medicines.html",
        {
            "user": request.session["user"],
            "medicines": safety["medicines"],
            "duplicate_count": safety["duplicate_count"],
            "interaction_count": safety["interaction_count"],
            "issue_count": safety["dose_count"] + safety["allergy_count"],
            "safety_status": safety["status"],
            "message": message,
            "success": success,
        },
    )


@login_required
def medicine_search_view(request):
    query = request.GET.get("q", "").strip()
    medicines = db.search_medicines(query, limit=20) if query else []

    return render(
        request,
        "medicine_search.html",
        {"user": request.session["user"], "query": query, "medicines": medicines},
    )


@login_required
def alternatives_view(request, product_id):
    medicine, alternatives = db.find_alternatives(product_id)

    return render(
        request,
        "alternatives.html",
        {
            "user": request.session["user"],
            "medicine": medicine,
            "alternatives": alternatives,
        },
    )


@login_required
def medication_safety_view(request):
    safety = db.medication_safety_check(email_of(request))

    return render(
        request,
        "medication_safety.html",
        {"user": request.session["user"], **safety},
    )


@login_required
def safety_report_pdf(request):
    """Download the medication-safety result as a PDF."""
    safety = db.medication_safety_check(email_of(request))
    pdf = build_safety_pdf(request.session["user"], safety)

    response = HttpResponse(pdf, content_type="application/pdf")
    response["Content-Disposition"] = (
        'attachment; filename="PharmaShield_Safety_Report.pdf"'
    )

    return response


@login_required
def profile_view(request):
    email = email_of(request)
    message = ""
    success = False

    if request.method == "POST":
        success, message = db.save_allergies(email, request.POST.get("allergies", ""))

    return render(
        request,
        "profile.html",
        {
            "user": request.session["user"],
            "allergies": ", ".join(db.get_allergies(email)),
            "message": message,
            "success": success,
        },
    )


# =========================================================
# VERIFY / HISTORY / REPORT
# =========================================================


@login_required
def verify_view(request):
    result = None
    message = None

    # POST from the form / scanner, or GET ?code=... (QR that holds a link)
    if request.method == "POST":
        code = request.POST.get("code", "")
    else:
        code = request.GET.get("code", "")

    code = code.strip()[:200]

    if code:
        result, message = db.verify(code, email_of(request))

    return render(
        request,
        "verify.html",
        {
            "user": request.session["user"],
            "result": result,
            "message": message,
            "code": code,
        },
    )


@login_required
def history_view(request):
    return render(
        request,
        "history.html",
        {"user": request.session["user"], "scans": db.scans(email_of(request))},
    )


@login_required
def report_view(request):
    message = ""

    if request.method == "POST":
        _, message = db.report(
            email_of(request),
            {
                "code": request.POST.get("code", ""),
                "reason": request.POST.get("reason", ""),
            },
        )

    return render(
        request,
        "report.html",
        {"user": request.session["user"], "message": message},
    )


# =========================================================
# ADMIN
# =========================================================

MEDICINE_FORM_KEYS = [
    "medicine_name", "brand_name", "generic_name", "primary_ingredient",
    "primary_strength", "active_ingredients", "num_active_ingredients",
    "therapeutic_class", "strength", "dosage_form", "manufacturer",
    "batch_number", "verification_code", "barcode", "qr_code",
    "manufacturing_date", "expiry_date", "mrp", "price_inr", "source",
    "source_url",
]


@admin_required
def medicine_db(request):
    message = ""

    if request.method == "POST":
        data = {key: request.POST.get(key, "") for key in MEDICINE_FORM_KEYS}
        _, message = db.add_medicine(data)

    return render(
        request,
        "medicine_db.html",
        {"user": request.session["user"], "message": message},
    )


# =========================================================
# ADMIN: DASHBOARD + QR GENERATOR
# =========================================================


@admin_required
def admin_dashboard(request):
    return render(
        request,
        "admin_dashboard.html",
        {"user": request.session["user"], "data": db.admin_overview()},
    )


@admin_required
def admin_qr(request):
    query = request.GET.get("q", "").strip()
    per_page = 48

    try:
        page = max(1, int(request.GET.get("page", "1")))
    except ValueError:
        page = 1

    batches, total = db.list_batches(query, limit=per_page, skip=(page - 1) * per_page)

    custom = request.GET.get("custom", "").strip()[:MAX_BYTES]

    return render(
        request,
        "admin_qr.html",
        {
            "user": request.session["user"],
            "batches": batches,
            "total": total,
            "query": query,
            "page": page,
            "has_prev": page > 1,
            "has_next": page * per_page < total,
            "custom": custom,
        },
    )


@admin_required
def qr_svg(request):
    """SVG QR code for ?data=...  (add &download=1 to save it as a file)."""
    data = request.GET.get("data", "")

    if not data or len(data.encode("utf-8")) > MAX_BYTES:
        return HttpResponse("Provide data (max %d bytes)." % MAX_BYTES, status=400)

    response = HttpResponse(make_svg(data), content_type="image/svg+xml")

    if request.GET.get("download"):
        safe = "".join(ch for ch in data if ch.isalnum() or ch in "-_")[:40] or "qr"
        response["Content-Disposition"] = f'attachment; filename="QR_{safe}.svg"'

    return response
