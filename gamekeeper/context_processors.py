from django.conf import settings
from django.contrib.auth import get_user_model

from .updates import get_update_status


def environment(request):
    """Expose the environment name and a banner flag to every template.

    Drives the non-production banner in base.html. Any environment other than
    'production' shows the banner; the name is displayed so self-hosters can
    label their own environments via DJANGO_ENV.
    """
    env = getattr(settings, 'ENVIRONMENT', 'production')
    return {
        'ENVIRONMENT': env,
        'SHOW_ENV_BANNER': env != 'production',
    }


def impersonation(request):
    """Expose the list of users a superuser may impersonate (issue #108).

    Only superusers get a non-empty list, and superusers are excluded as
    targets (mirrors IMPERSONATE.ALLOW_SUPERUSER=False), so the Tools-page
    picker can never offer a privilege-escalating target. Empty for everyone
    else — including while already impersonating, since the effective user is
    then the non-superuser target.
    """
    user = getattr(request, 'user', None)
    if user is None or not user.is_superuser:
        return {'impersonatable_users': []}
    users = (
        get_user_model().objects
        .filter(is_active=True, is_superuser=False)
        .order_by('username')
    )
    return {'impersonatable_users': users}


def update_notice(request):
    """Expose a 'newer GHCR image available' status to superusers (issue #95).

    Only superusers get the status (the navbar icon is a redeploy hint for whoever
    operates the instance); everyone else — including anonymous users and while
    impersonating a non-superuser — gets nothing, so the icon never renders for
    them. The status itself is a cached, best-effort GHCR check; it is None (and
    the icon stays hidden) in dev, when up to date, or when the check fails.
    """
    user = getattr(request, 'user', None)
    if user is None or not user.is_superuser:
        return {}
    return {'update_notice': get_update_status()}
