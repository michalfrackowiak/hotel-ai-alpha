"""Root URL configuration."""

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

from apps.web import views as web_views

admin.site.site_header = f"{settings.HOTEL['name']} · AI Reception"
admin.site.site_title = f"{settings.HOTEL['name']} Admin"
admin.site.index_title = "Operations"

urlpatterns = [
    path("", include("apps.web.urls")),
    path("admin/", admin.site.urls),
    path("i18n/", include("django.conf.urls.i18n")),
    path("api/v1/", include("apps.api.urls")),
    path("integrations/openline/webhook/", include("django_openline.urls")),
    path("healthz", web_views.healthz, name="healthz"),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
