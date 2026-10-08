from django.urls import path

from . import views

app_name = "tenancy"
urlpatterns = [
    path("branding/<str:kind>/", views.branding, name="branding"),
    path("", views.select, name="select"),
    path("switch/", views.switch, name="switch"),
    path("new/", views.onboard, name="onboard"),
    path("support/start/", views.support_start, name="support-start"),
    path("support/end/", views.support_end, name="support-end"),
    path("invite/<str:token>/", views.invitation_accept, name="invitation-accept"),
]
