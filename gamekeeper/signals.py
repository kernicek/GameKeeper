"""Signal receivers.

DESIGN §3: every user belongs to exactly one Group — a "group of one" is
auto-created on signup, and households form later by re-pointing Memberships.
Connected in apps.py ready().
"""

from django.conf import settings
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils.text import slugify

from .models import Group, Membership


@receiver(
    post_save, sender=settings.AUTH_USER_MODEL,
    dispatch_uid="gamekeeper_auto_group_of_one",
)
def create_group_of_one(sender, instance, created, raw=False, **kwargs):
    """Give a new user their own Group with an owner Membership (DESIGN §3).

    Skipped for fixture loads (raw) and for users that somehow already have a
    Membership (e.g. created before this signal existed and backfilled by
    import_mastersheet's fallback).
    """
    if raw or not created:
        return
    if Membership.objects.filter(user=instance).exists():
        return
    # Usernames are unique but slugs may still collide (a deleted user's
    # orphaned group, non-ASCII names slugifying to the same string) — suffix
    # until free rather than failing signup.
    base = slugify(instance.username) or f"user-{instance.pk}"
    slug = base
    counter = 2
    while Group.objects.filter(slug=slug).exists():
        slug = f"{base}-{counter}"
        counter += 1
    group = Group.objects.create(name=instance.username, slug=slug)
    Membership.objects.create(
        user=instance, group=group, role=Membership.Role.OWNER,
    )
