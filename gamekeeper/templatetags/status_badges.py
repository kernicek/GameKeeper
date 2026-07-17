"""Status→Bootstrap badge-colour filters (issue #11).

Single source of truth so the dashboard, purchase-detail page and any future
template render the same status in the same colour instead of hand-rolling
`{% if %}` chains that drift apart.
"""

from django import template

register = template.Library()

# Wave lifecycle gradient: grey → cyan → blue → amber → green, with red/dark
# for the two bad endings. Every status gets its own colour so the incoming-
# waves widget actually differentiates pending / pre-production / production.
_WAVE_STATUS_CLASSES = {
    "pending": "text-bg-secondary",
    "pre_production": "text-bg-info",
    "production": "text-bg-primary",
    "fulfilment": "text-bg-warning",
    "arrived": "text-bg-success",
    "never_arrived": "text-bg-danger",
    "cancelled": "text-bg-dark",
}

# Only "sent out" (action needed) and "filled out but still open" reach the
# dashboard PM widget; the rest fall through to secondary.
_PM_STATUS_CLASSES = {
    "sent_out": "text-bg-warning",
    "filled_out": "text-bg-info",
}

# Mirrors the canonical purchase table palette (partials/purchase_table.html).
_PURCHASE_STATUS_CLASSES = {
    "committed": "text-bg-success",
    "watching": "text-bg-info",
    "refunded": "text-bg-warning",
    "never_delivered": "text-bg-danger",
}

# BGG sync-diff categories, coloured least → most urgent: a suggestion to add
# (info), a diff to push out to BGG (warning), a real contradiction — an active
# copy BGG thinks is previously owned (danger), low-stakes archive cleanup
# (secondary), a new expansion available — good news, not a problem (success),
# issue #64, and a write-back push that failed outright (primary — distinct
# from the read-side diffs above), issue #117.
_SYNC_DIFF_CATEGORY_CLASSES = {
    "suggest_add": "text-bg-info",
    "missing_from_bgg": "text-bg-warning",
    "prev_owned_active": "text-bg-danger",
    "archived_on_bgg": "text-bg-secondary",
    "new_expansion": "text-bg-success",
    "push_failed": "text-bg-primary",
}


@register.filter
def wave_status_class(status):
    """Bootstrap ``text-bg-*`` class for a ``Wave.Status`` value."""
    return _WAVE_STATUS_CLASSES.get(status, "text-bg-secondary")


@register.filter
def pm_status_class(status):
    """Bootstrap ``text-bg-*`` class for a ``PledgeManagerStatus`` value."""
    return _PM_STATUS_CLASSES.get(status, "text-bg-secondary")


@register.filter
def purchase_status_class(status):
    """Bootstrap ``text-bg-*`` class for a ``Purchase.Status`` value."""
    return _PURCHASE_STATUS_CLASSES.get(status, "text-bg-secondary")


@register.filter
def sync_diff_category_class(category):
    """Bootstrap ``text-bg-*`` class for a ``BggSyncDiff.Category`` value."""
    return _SYNC_DIFF_CATEGORY_CLASSES.get(category, "text-bg-secondary")
