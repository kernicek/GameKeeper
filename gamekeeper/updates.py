"""GHCR "newer image available" check (issue #95).

The app ships as a GHCR image deployed via Portainer; nothing inside the running
container otherwise knows a newer image has been published. This module compares
the running image (its version baked in at build time as settings.APP_VERSION)
against what's published on GHCR, so a superuser navbar icon can flag that a
redeploy is available.

Comparison is by manifest *digest*, not by tag name: the running tag and :latest
are resolved to their content digests and compared. Digests are robust to the
repo's mixed tag schemes (v5, 2026.07.05, ...) and even catch a re-push of the
same version tag — the only question that matters is "is the image I'm running
the same bytes as :latest?".

Auth: for a public package the anonymous ghcr.io token-exchange suffices (no
secret). While the package is private (issue #100) an optional read:packages
token (settings.GHCR_TOKEN) is used instead. Any failure is swallowed and the
notice simply stays hidden — this is a convenience, never a hard dependency.
"""

import base64
import logging

import requests
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

# Manifest media types we'll accept when resolving a tag to its digest — covers
# both single-arch manifests and multi-arch indexes, Docker and OCI flavours.
_MANIFEST_ACCEPT = ", ".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.docker.distribution.manifest.v2+json",
])
_TIMEOUT = 10  # seconds; a slow registry must never stall a page render.

# Cache key + TTLs. A successful check is cheap to keep for an hour; a failure
# (or "unknown") is re-tried sooner so a transient registry blip self-heals
# without hammering GHCR on every superuser request.
_CACHE_KEY = "ghcr_update_check"
_SUCCESS_TTL = 60 * 60
_FAILURE_TTL = 5 * 60
# Guard the (rare) name-resolution loop against a pathologically long tag list.
_MAX_TAGS_SCANNED = 50


def _split_image(image):
    """Split 'ghcr.io/owner/name' into (registry_host, 'owner/name')."""
    host, _, repository = image.partition("/")
    return host, repository


def _bearer(session, host, repository):
    """Return the bearer token string for pull-scoped registry v2 calls.

    With a configured GHCR_TOKEN (private package), GHCR accepts the base64 of
    the PAT as the bearer directly. Otherwise fetch an anonymous pull token from
    the registry's token endpoint (works for public packages, no secret).
    """
    token = getattr(settings, "GHCR_TOKEN", "")
    if token:
        return base64.b64encode(token.encode()).decode()
    resp = session.get(
        f"https://{host}/token",
        params={"scope": f"repository:{repository}:pull"},
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()["token"]


def _digest(session, host, repository, tag, auth):
    """Return the content digest for a tag, or None if it doesn't exist."""
    resp = session.head(
        f"https://{host}/v2/{repository}/manifests/{tag}",
        headers={"Accept": _MANIFEST_ACCEPT, "Authorization": f"Bearer {auth}"},
        timeout=_TIMEOUT,
    )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.headers.get("Docker-Content-Digest")


def _name_latest(session, host, repository, auth, latest_digest, running):
    """Best-effort: find the version tag whose digest matches :latest.

    :latest points at the same manifest as the newest version tag, but the tags
    list only carries names, so we HEAD candidates until one matches. Returns the
    tag name, or None if it can't be resolved within the scan cap.
    """
    resp = session.get(
        f"https://{host}/v2/{repository}/tags/list",
        headers={"Authorization": f"Bearer {auth}"},
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    tags = resp.json().get("tags") or []
    candidates = [t for t in tags if t not in ("latest", running)]
    for tag in candidates[:_MAX_TAGS_SCANNED]:
        if _digest(session, host, repository, tag, auth) == latest_digest:
            return tag
    return None


def check_for_update():
    """Query GHCR and report whether a newer image than the running one exists.

    Returns a dict {'update_available': bool, 'running': str, 'latest': str|None}
    on a successful check, or None if the check can't be completed (misconfigured,
    network/registry error) — callers treat None as "don't show the notice".
    """
    running = getattr(settings, "APP_VERSION", "")
    image = getattr(settings, "GHCR_IMAGE", "")
    if not running or not image:
        return None

    host, repository = _split_image(image)
    session = requests.Session()
    try:
        auth = _bearer(session, host, repository)
        latest_digest = _digest(session, host, repository, "latest", auth)
        running_digest = _digest(session, host, repository, running, auth)
        if latest_digest is None:
            return None
        if latest_digest == running_digest:
            return {"update_available": False, "running": running, "latest": running}
        latest = _name_latest(session, host, repository, auth, latest_digest, running)
        return {"update_available": True, "running": running, "latest": latest}
    except (requests.RequestException, ValueError, KeyError) as exc:
        logger.warning("GHCR update check failed: %s", exc)
        return None
    finally:
        session.close()


def get_update_status():
    """Cached wrapper around check_for_update().

    Runs the real check at most once per TTL; a superuser page view on a cache
    miss pays one (short-timeout) round trip, everyone else reads the cache.
    Returns the same dict as check_for_update(), or None.
    """
    if not getattr(settings, "APP_VERSION", ""):
        return None
    sentinel = object()
    cached = cache.get(_CACHE_KEY, sentinel)
    if cached is not sentinel:
        return cached
    result = check_for_update()
    cache.set(_CACHE_KEY, result, _SUCCESS_TTL if result else _FAILURE_TTL)
    return result
