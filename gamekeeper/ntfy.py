"""Push notifications to a self-hosted ntfy server (issue #162), complementing
the DESIGN §11 email reminders. One instance-wide server URL (settings.
NTFY_SERVER_URL); each user has their own topic (Membership.ntfy_topic) so
reminders don't cross between users on a shared deployment.
"""

import logging

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

TIMEOUT = 10


def send_ntfy(topic, title, message):
    """POST a push notification to the configured ntfy server. Fail-soft:
    never raises, and no-ops when NTFY_SERVER_URL or topic isn't set — the
    caller (Celery beat) must not break if ntfy is unconfigured or
    unreachable. Returns whether the push was actually sent."""
    server_url = settings.NTFY_SERVER_URL
    if not server_url or not topic:
        logger.info(
            "send_ntfy no-op: %s not set.",
            "NTFY_SERVER_URL" if not server_url else "topic",
        )
        return False
    url = f"{server_url.rstrip('/')}/{topic}"
    headers = {"Title": title}
    if settings.NTFY_AUTH_TOKEN:
        headers["Authorization"] = f"Bearer {settings.NTFY_AUTH_TOKEN}"
    try:
        response = requests.post(
            url,
            data=message.encode("utf-8"),
            headers=headers,
            timeout=TIMEOUT,
        )
        response.raise_for_status()
    except requests.RequestException:
        logger.warning("ntfy push to topic %r failed.", topic, exc_info=True)
        return False
    return True
