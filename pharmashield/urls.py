from django.urls import path
from django.views.generic import RedirectView

from core import views


urlpatterns = [
    # browsers ask for /favicon.ico by default
    path(
        "favicon.ico",
        RedirectView.as_view(url="/static/favicon/favicon.ico?v=4", permanent=False),
    ),
    path("", views.login_view, name="login"),
    path("register/", views.register_view, name="register"),
    path("forgot-password/", views.forgot_password, name="forgot_password"),
    path("reset-password/<str:token>/", views.reset_password, name="reset_password"),
    path("dashboard/", views.dashboard, name="dashboard"),
    path("my-medicines/", views.my_medicines_view, name="my_medicines"),
    path("medicine-search/", views.medicine_search_view, name="medicine_search"),
    path(
        "alternatives/<str:product_id>/",
        views.alternatives_view,
        name="alternatives",
    ),
    path("medication-safety/", views.medication_safety_view, name="medication_safety"),
    path("medication-safety/report.pdf", views.safety_report_pdf, name="safety_report"),
    path("profile/", views.profile_view, name="profile"),
    path("verify/", views.verify_view, name="verify"),
    path("history/", views.history_view, name="history"),
    path("medicine-db/", views.medicine_db, name="medicine_db"),
    path("report/", views.report_view, name="report"),
    path("logout/", views.logout_view, name="logout"),
    path("keepalive/", views.keepalive, name="keepalive"),
    path("admin-dashboard/", views.admin_dashboard, name="admin_dashboard"),
    path("admin-qr/", views.admin_qr, name="admin_qr"),
    path("qr.svg", views.qr_svg, name="qr_svg"),
]
