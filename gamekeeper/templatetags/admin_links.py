"""Template tags for linking into the Django admin (issue #52)."""

from django import template
from django.urls import NoReverseMatch, reverse

register = template.Library()


@register.inclusion_tag("partials/_admin_edit_link.html", takes_context=True)
def admin_edit_link(context, obj):
    """Render a superuser-only link to ``obj``'s admin change page.

    Works for any registered model: the admin URL name is built generically
    from the object's ``app_label`` and ``model_name``. Returns no URL (and
    the partial renders nothing) for anonymous/non-superuser users or when the
    object has no admin registration.
    """
    user = context.get("user")
    if obj is None or user is None or not user.is_superuser:
        return {"url": None}
    meta = obj._meta
    try:
        url = reverse(
            f"admin:{meta.app_label}_{meta.model_name}_change", args=[obj.pk]
        )
    except NoReverseMatch:
        url = None
    return {"url": url}
