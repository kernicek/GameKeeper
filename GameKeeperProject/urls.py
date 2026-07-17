"""URL configuration for GameKeeperProject."""
from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path('admin/', admin.site.urls),
    path('accounts/', include('django.contrib.auth.urls')),
    # Superuser impersonation start/stop (issue #108). Access is gated by the
    # IMPERSONATE settings (superuser-only) inside the library's views.
    path('impersonate/', include('impersonate.urls')),
    path('', include('gamekeeper.urls')),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
