"""Views: the needs-attention dashboard and cull-candidates curation view
(DESIGN §11), the sleeves workbench (§5), the purchase-pipeline browse view
(§6), the GameChooser collection grid (DESIGN §10/§13) and the
image-forward game detail page (§13). Everything sits behind the auth wall
(DESIGN §3) except the /share/<token>/ views — the tier-4 anonymous
projection, gated by the group's unguessable share_token instead. The
/g/<slug>/ views are the logged-in viewer surface for tiers 2 (ShareGrant)
and 3 (server-public), sharing the same curated projection."""

import datetime
import logging
import re
import unicodedata
from collections import defaultdict
from decimal import Decimal, InvalidOperation

import requests

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required, user_passes_test
from django.core.files.base import ContentFile
from django.core.mail import send_mail
from django.db import transaction
from django.db.models import Count, F, Max, Q
from django.http import Http404, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.http import require_POST

from . import bgg_sync, ntfy
from .bgg import extract_bgg_id
from .forms import (
    COVER_EXTENSIONS, CopyForm, EditionForm, FamilyForm, GameForm,
    PledgePlanBundleForm, PledgePlanForm, PledgePlanItemForm, ProductForm,
    PurchaseForm, SeriesForm, WaveFormSet, _validate_cover_image,
)
from .models import (
    BggLink, BggSyncDiff, CardSize, Copy, CopySleeveStatus, Document, Edition,
    ExpansionSighting, ExternalLink, Family, Game, GameType, Group, Invite,
    Loan, Location, Membership, PledgePlan, PledgePlanBundle, PledgePlanItem,
    Play, Product, Purchase, Series, ShareGrant,
    SleeveInventory, SleeveProduct, SleeveRequirement, Tag, ToolRun, Wave,
    WishlistEntry, sleeve_shortfall,
)
from .tasks import (
    REMINDER_WINDOW_DAYS, push_bgg_fortrade_task, push_bgg_status_task,
    run_tool_command,
)

logger = logging.getLogger(__name__)

# The app is single-user behind @login_required; the Tools page (issue #90)
# is the one superuser-only surface. Only precedent for the check is
# templatetags/admin_links.py.
superuser_required = user_passes_test(lambda u: u.is_superuser)

# Wave statuses that still have something on the way.
INCOMING_STATUSES = (
    Wave.Status.PENDING,
    Wave.Status.PRE_PRODUCTION,
    Wave.Status.PRODUCTION,
    Wave.Status.FULFILMENT,
)

# One row cap shared by every §11 dashboard card (issue #83): cards show at
# most this many rows with a "showing N of M" hint, and each header links to a
# full-list page that renders the same shared partial uncapped.
DASHBOARD_CARD_LIMIT = 20


def _shortfall_context(request, show_covered=None, limit=None):
    # The dashboard widget shows only actual shortfalls; the /sleeves/ page
    # keeps covered sizes visible too (its preorder toggle re-requests the
    # partial with ?all=1 to preserve that). The dashboard caps at ``limit``
    # (issue #83) with the true total in ``shortfall_total`` for the hint.
    include_preorders = bool(request.GET.get("preorders"))
    if show_covered is None:
        show_covered = bool(request.GET.get("all"))
    rows = [
        entry for entry in sleeve_shortfall(request.user, include_preorders)
        if show_covered or entry["shortfall"]
    ]
    return {
        "shortfall": rows[:limit] if limit else rows,
        "shortfall_total": len(rows),
        "include_preorders": include_preorders,
        "show_covered": show_covered,
    }


def _sync_diff_context(user, limit=None):
    """Unreviewed BGG sync diffs for the §11 widget — dismissed rows are
    hidden for good (until the sync sees the diff resolve and reappear). The
    dashboard card caps at ``limit`` (issue #83) with the true total kept in
    ``sync_diffs_total`` for the "showing N of M" hint; the full-list page and
    the reminder count pass no limit."""
    diffs = (
        BggSyncDiff.objects.filter(owner=user, dismissed_at__isnull=True)
        .select_related("game")
    )
    total = diffs.count()
    return {
        "sync_diffs": diffs[:limit] if limit else diffs,
        "sync_diffs_total": total,
    }


def _new_expansion_context(user, limit=None):
    """Unreviewed new-expansion diffs for the §11/§8 widget (issue #64) —
    same shape as _sync_diff_context, just a different BggSyncDiff category."""
    diffs = (
        BggSyncDiff.objects.filter(
            owner=user, category=BggSyncDiff.Category.NEW_EXPANSION,
            dismissed_at__isnull=True,
        )
        .select_related("game")
    )
    total = diffs.count()
    return {
        "new_expansions": diffs[:limit] if limit else diffs,
        "new_expansions_total": total,
    }


def _incoming_rows(user):
    """Incoming waves for the §11 card / full-list page (issue #83) — soonest
    ETA on top; waves with no date information sink to the bottom."""
    incoming = (
        Wave.objects.filter(
            purchase__owner=user,
            purchase__status=Purchase.Status.COMMITTED,
            status__in=INCOMING_STATUSES,
        )
        .select_related("purchase")
    )
    return sorted(
        incoming,
        key=lambda wave: (
            wave.expected_arrival or wave.original_eta
            or wave.purchase.ordered_date or datetime.date.max
        ),
    )


def _to_craft_rows(user):
    """Not-ready-to-play copies for the §11 "to craft" card / full-list page
    (issue #19) — currently only produced by an unprinted PnP arrival, oldest
    acquired first (the ones waiting longest surface at the top)."""
    return (
        Copy.objects.filter(
            owner=user,
            archive_status=Copy.ArchiveStatus.ACTIVE,
            ready_status=Copy.ReadyStatus.NOT_READY,
        )
        .select_related("edition__game")
        .order_by("acquired_date")
    )


def _pm_action_rows(user):
    """Pledge managers needing action for the §11 card / full-list page."""
    today = timezone.localdate()
    pm_actions = (
        Purchase.objects.filter(owner=user)
        .filter(Q(waves__status__in=INCOMING_STATUSES))
        .filter(
            # "Sent out" PMs need action; "Not yet" ones wait on the creator,
            # not on us (#59). A "Filled out" PM whose close date is still
            # ahead isn't fully done either — the pledge can still be revised
            # until it closes (#85). A filled-out PM with no known close date,
            # or one already past, drops off.
            Q(pledge_manager_status=Purchase.PledgeManagerStatus.SENT_OUT)
            | Q(
                pledge_manager_status=Purchase.PledgeManagerStatus.FILLED_OUT,
                pledge_manager_close_date__gte=today,
            )
        )
        .distinct()
    )
    # Still-actionable "Sent out" PMs on top; "Filled out but still open"
    # revisable reminders at the bottom (#85). Within each group, soonest
    # close date first, and date-less PMs trail below the dated ones (#135).
    return sorted(
        pm_actions,
        key=lambda p: (
            p.pledge_manager_status != Purchase.PledgeManagerStatus.SENT_OUT,
            p.pledge_manager_close_date or datetime.date.max,
        ),
    )


def _ending_soon_rows(user):
    """Campaigns ending soon: watched-but-unbacked with a known end date still
    ahead (§6/§11) — back it before the campaign ends or let it go. Deadlines
    inside the email reminder window get the warning badge in the template."""
    today = timezone.localdate()
    return [
        {"purchase": p, "days_left": (p.campaign_end_date - today).days}
        for p in Purchase.objects.filter(
            owner=user,
            status=Purchase.Status.WATCHING,
            campaign_end_date__gte=today,
        ).order_by("campaign_end_date")
    ]


def _undated_watching_count(user):
    """Watched campaigns without an end date can't appear in "ending soon" (or
    the reminder emails), so their count renders as a fill-it-in nudge."""
    return Purchase.objects.filter(
        owner=user,
        status=Purchase.Status.WATCHING,
        campaign_end_date=None,
    ).count()


@login_required
def dashboard(request):
    """DESIGN §11 "needs attention": sleeve shortfall, incoming waves,
    pledge managers needing action, watched campaigns ending soon, unreviewed
    BGG sync diffs and new expansions of owned base games (§8, issue #64).
    Each card caps at DASHBOARD_CARD_LIMIT rows with a "showing N of M" hint
    and a header link to the card's full-list page (issue #83)."""
    incoming = _incoming_rows(request.user)
    pm_actions = _pm_action_rows(request.user)
    ending_soon = _ending_soon_rows(request.user)
    to_craft = list(_to_craft_rows(request.user))

    return render(request, "dashboard.html", {
        "incoming": incoming[:DASHBOARD_CARD_LIMIT],
        "incoming_total": len(incoming),
        "pm_actions": pm_actions[:DASHBOARD_CARD_LIMIT],
        "pm_actions_total": len(pm_actions),
        "ending_soon": ending_soon[:DASHBOARD_CARD_LIMIT],
        "ending_soon_total": len(ending_soon),
        "to_craft": to_craft[:DASHBOARD_CARD_LIMIT],
        "to_craft_total": len(to_craft),
        "undated_watching": _undated_watching_count(request.user),
        "window_days": REMINDER_WINDOW_DAYS,
        "card_limit": DASHBOARD_CARD_LIMIT,
        "wishlist_priority_choices": Game.WishlistPriority.choices,
        **_shortfall_context(request, limit=DASHBOARD_CARD_LIMIT),
        **_sync_diff_context(request.user, limit=DASHBOARD_CARD_LIMIT),
        **_new_expansion_context(request.user, limit=DASHBOARD_CARD_LIMIT),
    })


@login_required
def dashboard_incoming_waves(request):
    """Full, uncapped list of incoming waves (issue #83) — shares
    partials/incoming_table.html with the dashboard card."""
    return render(request, "dashboard_incoming_waves.html", {
        "incoming": _incoming_rows(request.user),
    })


@login_required
def dashboard_to_craft(request):
    """Full, uncapped list of not-ready-to-play copies (issue #19) — shares
    partials/to_craft_table.html with the dashboard card."""
    return render(request, "dashboard_to_craft.html", {
        "to_craft": _to_craft_rows(request.user),
    })


@login_required
@require_POST
def copy_mark_ready(request, pk):
    """Flip a not-ready copy back to ready (issue #19) — e.g. once a PnP
    copy is printed/prepped. Named generically since the field isn't
    PnP-specific, even though PnP prep is the only producer of NOT_READY
    copies today. Re-renders the to-craft table the button lives in."""
    copy = get_object_or_404(
        Copy, pk=pk, owner=request.user,
        ready_status=Copy.ReadyStatus.NOT_READY,
    )
    copy.ready_status = Copy.ReadyStatus.READY
    copy.save(update_fields=["ready_status", "updated_at"])

    return render(request, "partials/to_craft_table.html", {
        "to_craft": _to_craft_rows(request.user),
    })


@login_required
def dashboard_pledge_managers(request):
    """Full, uncapped list of pledge managers needing action (issue #83)."""
    return render(request, "dashboard_pledge_managers.html", {
        "pm_actions": _pm_action_rows(request.user),
    })


@login_required
def dashboard_campaigns_ending(request):
    """Full, uncapped list of watched campaigns ending soon (issue #83)."""
    return render(request, "dashboard_campaigns_ending.html", {
        "ending_soon": _ending_soon_rows(request.user),
        "undated_watching": _undated_watching_count(request.user),
        "window_days": REMINDER_WINDOW_DAYS,
    })


@login_required
def dashboard_sync_diffs(request):
    """Full, uncapped list of unreviewed BGG sync diffs (issue #83) — reuses
    the dashboard widget with its Dismiss buttons, in full scope."""
    return render(request, "dashboard_sync_diffs.html", {
        "full_scope": True,
        **_sync_diff_context(request.user),
    })


@login_required
def dashboard_new_expansions(request):
    """Full, uncapped list of unreviewed new-expansion diffs (issue #64) —
    reuses the dashboard widget with its Dismiss/Add-to-wishlist actions, in
    full scope."""
    return render(request, "dashboard_new_expansions.html", {
        "full_scope": True,
        "wishlist_priority_choices": Game.WishlistPriority.choices,
        **_new_expansion_context(request.user),
    })


@login_required
@require_POST
def new_expansion_dismiss(request, pk):
    """§8 dismiss/mark-seen for a new-expansion diff — same "not interested,
    never nag again" semantics as sync_diff_dismiss (kept separate so that
    already-tested view doesn't need to branch on category)."""
    diff = get_object_or_404(
        BggSyncDiff, pk=pk, owner=request.user,
        category=BggSyncDiff.Category.NEW_EXPANSION, dismissed_at__isnull=True,
    )
    diff.dismissed_at = timezone.now()
    diff.save(update_fields=["dismissed_at"])
    full_scope = request.GET.get("full") == "1"
    limit = None if full_scope else DASHBOARD_CARD_LIMIT
    return render(
        request, "partials/new_expansion_widget.html",
        {
            "full_scope": full_scope,
            "card_limit": DASHBOARD_CARD_LIMIT,
            "wishlist_priority_choices": Game.WishlistPriority.choices,
            **_new_expansion_context(request.user, limit=limit),
        },
    )


@login_required
@require_POST
def wishlist_add(request, pk):
    """Add a discovered expansion to the owner's wishlist (issue #64) — the
    diff row (owner-scoped) is the trusted source for the expansion's bgg
    id/name, not raw POST data. Creates a stub Game + primary BggLink if none
    resolves that bgg id yet (a later full sync fills in the rest), backfills
    any matching ExpansionSighting, and dismisses the diff too: DESIGN §8
    frames dismiss and add-to-wishlist as alternative ways to handle a row."""
    diff = get_object_or_404(
        BggSyncDiff, pk=pk, owner=request.user,
        category=BggSyncDiff.Category.NEW_EXPANSION, dismissed_at__isnull=True,
    )
    priority = request.POST.get("priority")
    if priority not in {str(value) for value, _ in Game.WishlistPriority.choices}:
        return HttpResponseBadRequest("Unknown wishlist priority.")

    link = BggLink.objects.filter(bgg_id=diff.bgg_id).select_related("game").first()
    if link:
        game = link.game
    else:
        game = Game.objects.create(
            name=diff.bgg_name, bgg_name=diff.bgg_name, type=Game.Type.EXPANSION,
        )
        BggLink.objects.create(game=game, bgg_id=diff.bgg_id, is_primary=True)
    ExpansionSighting.objects.filter(
        bgg_id=diff.bgg_id, expansion__isnull=True,
    ).update(expansion=game)
    WishlistEntry.objects.update_or_create(
        owner=request.user, game=game, defaults={"priority": priority},
    )
    # Issue #117: wishlisting locally pushes the same status to BGG.
    _enqueue_bgg_push(
        game, Game.BggCollectionStatus.WISHLIST, request.user, priority=int(priority),
    )

    diff.dismissed_at = timezone.now()
    diff.save(update_fields=["dismissed_at"])
    full_scope = request.GET.get("full") == "1"
    limit = None if full_scope else DASHBOARD_CARD_LIMIT
    return render(
        request, "partials/new_expansion_widget.html",
        {
            "full_scope": full_scope,
            "card_limit": DASHBOARD_CARD_LIMIT,
            "wishlist_priority_choices": Game.WishlistPriority.choices,
            **_new_expansion_context(request.user, limit=limit),
        },
    )


@login_required
def wishlist_list(request):
    """Read-only list of the owner's wishlist entries (issue #64) — so
    WishlistEntry isn't a write-only black hole. Ready for a follow-up issue
    to populate this from an automatic BGG wishlist sync."""
    return render(request, "wishlist.html", {
        "entries": WishlistEntry.objects.filter(owner=request.user).select_related("game"),
    })


@login_required
@require_POST
def wishlist_remove(request, pk):
    """Drop a wishlist entry (issue #117) — the first general-purpose removal
    path (wishlist_add only ever creates). Only pushes the removal to BGG
    when wishlist is actually the game's currently tracked BGG status —
    dropping a stale WishlistEntry for a game that's since become
    own/prev_owned/preordered on BGG must not clear that."""
    entry = get_object_or_404(WishlistEntry, pk=pk, owner=request.user)
    game = entry.game
    entry.delete()
    if game.bgg_collection_status == Game.BggCollectionStatus.WISHLIST:
        _enqueue_bgg_push(game, "", request.user)
    return render(request, "partials/wishlist_table.html", {
        "entries": WishlistEntry.objects.filter(owner=request.user).select_related("game"),
    })


@login_required
@require_POST
def sync_diff_dismiss(request, pk):
    """§8 dismiss/mark-seen: per-user "not interested, never nag again" —
    the row survives (so re-syncs don't resurrect the nag) but leaves the
    widget. Owner-scoped 404, already-dismissed rows 404 too; the whole
    widget re-renders so the header count stays honest. ``?full=1`` (from the
    full-list page, issue #83) re-renders uncapped; the dashboard re-renders
    capped so its "showing N of M" hint stays honest."""
    diff = get_object_or_404(
        BggSyncDiff, pk=pk, owner=request.user, dismissed_at__isnull=True,
    )
    diff.dismissed_at = timezone.now()
    diff.save(update_fields=["dismissed_at"])
    full_scope = request.GET.get("full") == "1"
    limit = None if full_scope else DASHBOARD_CARD_LIMIT
    return render(
        request, "partials/sync_diff_widget.html",
        {
            "full_scope": full_scope,
            "card_limit": DASHBOARD_CARD_LIMIT,
            **_sync_diff_context(request.user, limit=limit),
        },
    )


# Issue #168's per-category accept handlers, scoped to the two categories
# where BGG's status disagrees with an actual Copy the app can mutate.
_SYNC_DIFF_ACCEPT_HANDLERS = {
    BggSyncDiff.Category.PREV_OWNED_ACTIVE: bgg_sync.accept_prev_owned_active,
    BggSyncDiff.Category.ARCHIVED_ON_BGG: bgg_sync.accept_archived_on_bgg,
}


@login_required
@require_POST
def sync_diff_accept(request, pk):
    """Issue #168's "update GameKeeper to match BGG" action: the missing
    BGG-to-app pull direction (#157 proved the app-to-BGG push direction
    works). Scoped to PREV_OWNED_ACTIVE/ARCHIVED_ON_BGG — the only
    categories with a real Copy on our side to mutate. Deletes the diff
    outright rather than dismissing it: the condition it flagged is now
    resolved, same as a diff that resolves between sync runs."""
    diff = get_object_or_404(
        BggSyncDiff, pk=pk, owner=request.user,
        category__in=_SYNC_DIFF_ACCEPT_HANDLERS, dismissed_at__isnull=True,
    )
    _SYNC_DIFF_ACCEPT_HANDLERS[diff.category](request.user, diff.game)
    diff.delete()
    full_scope = request.GET.get("full") == "1"
    limit = None if full_scope else DASHBOARD_CARD_LIMIT
    return render(
        request, "partials/sync_diff_widget.html",
        {
            "full_scope": full_scope,
            "card_limit": DASHBOARD_CARD_LIMIT,
            **_sync_diff_context(request.user, limit=limit),
        },
    )


@login_required
def shortfall_partial(request):
    """htmx target for the include-preorders toggle (DESIGN §5): swaps the
    shortfall table without a full reload. The dashboard widget (no ?all) caps
    at DASHBOARD_CARD_LIMIT with a "showing N of M" hint; the /sleeves/ page
    (?all=1) shows every size uncapped (issue #83)."""
    show_covered = bool(request.GET.get("all"))
    limit = None if show_covered else DASHBOARD_CARD_LIMIT
    return render(request, "partials/shortfall_table.html", {
        "card_limit": DASHBOARD_CARD_LIMIT,
        **_shortfall_context(request, limit=limit),
    })


# Status sort order on the sleeves page: work first, done last.
SLEEVE_STATUS_ORDER = {
    CopySleeveStatus.Status.TO_SLEEVE: 0,
    CopySleeveStatus.Status.NOT_SLEEVED: 1,
    CopySleeveStatus.Status.SLEEVED: 2,
}


def _sleeve_row(copy, requirement, status, products):
    """One row for the shared sleeve table (partials/sleeve_table.html), used
    by the §5 worklist, the game-detail read-only card, and the copy-edit
    editable card (issue #17). A slot with no CopySleeveStatus row yet renders
    (and edits) as not-sleeved. Every row is anchored to a SleeveRequirement
    (issue #3) — a status can't outlive its requirement, so there's no
    unknown-count case to handle. Carries both the raw status value +
    product_id (editable selects) and the display label + product object
    (read-only cells) so the one partial serves every site."""
    default = CopySleeveStatus.Status.NOT_SLEEVED
    return {
        "copy": copy,
        "requirement": requirement,
        "card_size": requirement.card_size,
        "count": requirement.count,
        "status": status.status if status else default,
        "status_label": status.get_status_display() if status else default.label,
        "product": status.product if status else None,
        "product_id": status.product_id if status else None,
        "products": products,
    }


def _sleeve_inventory_context(user):
    """Every known SleeveProduct with the owner's stock (DESIGN §5) — rows
    without an inventory record render as zeros and get one on first edit."""
    inventories = {
        inventory.product_id: inventory
        for inventory in SleeveInventory.objects.filter(owner=user)
    }
    return {
        "inventory_rows": [
            {"product": product, "inventory": inventories.get(product.pk)}
            for product in SleeveProduct.objects.select_related("card_size")
            .order_by("card_size__width_mm", "card_size__height_mm", "brand", "name")
        ],
    }


def _sleeve_status_context(user, data):
    """Per-copy, per-SleeveRequirement sleeving worklist (DESIGN §5): the
    owner's active copies crossed with their editions' sleeve requirements,
    joined with the CopySleeveStatus rows. Slots without a status row yet
    render (and edit) as not-sleeved."""
    # The status filter is named show (not status) so the hx-include'd
    # filter form can never collide with the row selects' status edit param
    # (same convention as curation's show_immune).
    filters = {
        "status": data.get("show") or "",
        "size": _parse_int(data.get("size")),
    }

    copies = Copy.objects.filter(
        owner=user, archive_status=Copy.ArchiveStatus.ACTIVE,
    ).select_related("edition__game")

    requirements = defaultdict(list)
    for requirement in SleeveRequirement.objects.filter(
        edition__copies__owner=user,
    ).select_related("card_size").distinct():
        requirements[requirement.edition_id].append(requirement)

    statuses = defaultdict(dict)
    for status in CopySleeveStatus.objects.filter(
        copy__owner=user, copy__archive_status=Copy.ArchiveStatus.ACTIVE,
    ).select_related("requirement__card_size"):
        statuses[status.copy_id][status.requirement_id] = status

    products_by_size = defaultdict(list)
    for product in SleeveProduct.objects.order_by("brand", "name"):
        products_by_size[product.card_size_id].append(product)

    rows = []

    for copy in copies:
        for requirement in requirements.get(copy.edition_id, []):
            status = statuses[copy.pk].get(requirement.pk)
            rows.append(_sleeve_row(
                copy, requirement, status,
                products_by_size.get(requirement.card_size_id, []),
            ))

    sizes = sorted(
        {row["card_size"].pk: row["card_size"] for row in rows}.values(),
        key=lambda size: (size.width_mm, size.height_mm),
    )
    total_count = len(rows)
    if filters["status"] in CopySleeveStatus.Status.values:
        rows = [row for row in rows if row["status"] == filters["status"]]
    if filters["size"] is not None:
        rows = [row for row in rows if row["card_size"].pk == filters["size"]]
    rows.sort(key=lambda row: (
        SLEEVE_STATUS_ORDER[row["status"]],
        row["copy"].edition.game.name,
        (row["card_size"].width_mm, row["card_size"].height_mm),
    ))

    return {
        "sleeve_rows": rows,
        "sleeve_row_count": len(rows),
        "sleeve_total_count": total_count,
        "sleeve_filters": filters,
        "sleeve_sizes": sizes,
        "sleeve_status_choices": CopySleeveStatus.Status.choices,
    }


def _copy_sleeve_rows(copy, edition=None):
    """Size-ordered sleeve rows for a single copy (issue #17): the same
    requirement-joined-with-status logic as the §5 worklist, scoped to one
    copy. Reuses the copy's already-loaded edition when the caller passes it
    (game_detail prefetches editions), else falls back to the FK."""
    edition = edition or copy.edition
    products_by_size = defaultdict(list)
    for product in SleeveProduct.objects.order_by("brand", "name"):
        products_by_size[product.card_size_id].append(product)
    statuses = {
        status.requirement_id: status
        for status in copy.sleeve_statuses.select_related("requirement", "product")
    }
    rows = []
    for requirement in edition.sleeve_requirements.select_related("card_size").all():
        rows.append(_sleeve_row(
            copy, requirement, statuses.get(requirement.pk),
            products_by_size.get(requirement.card_size_id, []),
        ))
    rows.sort(key=lambda row: (row["card_size"].width_mm, row["card_size"].height_mm))
    return rows


def _copy_sleeve_table_context(copy):
    """Include params for the editable per-copy sleeve table (issue #17): the
    copy edit card and its in-place re-render both render sleeve_table.html
    with these, so a status change swaps just this copy's rows back in."""
    return {
        "rows": _copy_sleeve_rows(copy),
        "editable": True,
        "show_game": False,
        "hx_target": "#copy-sleeves",
        "scope": "copy",
        "status_choices": CopySleeveStatus.Status.choices,
    }


@login_required
def sleeves(request):
    """The §5 sleeves workbench: the full shortfall math (covered sizes
    included, preorder toggle), pack inventory editable in place, and the
    per-copy sleeving worklist that feeds the whole computation. htmx GETs
    come from the worklist filter form and swap just that table."""
    status_context = _sleeve_status_context(request.user, request.GET)
    if request.headers.get("HX-Request"):
        return render(request, "partials/sleeve_status_table.html", status_context)
    return render(request, "sleeves.html", {
        **_shortfall_context(request, show_covered=True),
        **_sleeve_inventory_context(request.user),
        **status_context,
    })


@login_required
@require_POST
def sleeve_inventory_edit(request, product_pk):
    """In-place pack/loose stock editing on the inventory table (DESIGN §5).
    Inventory is keyed (owner, product) — the row is created on first edit,
    so every catalog product is editable without admin setup."""
    product = get_object_or_404(SleeveProduct, pk=product_pk)

    updates = {}
    for field in ("packs", "loose"):
        if field in request.POST:
            raw = request.POST[field].strip()
            value = _parse_int(raw) if raw else 0
            if value is None or value < 0:
                return HttpResponseBadRequest(f"{field} must be a non-negative number.")
            updates[field] = value
    if updates:
        SleeveInventory.objects.update_or_create(
            owner=request.user, product=product, defaults=updates,
        )

    return render(request, "partials/sleeve_inventory_table.html",
                  _sleeve_inventory_context(request.user))


@login_required
@require_POST
def sleeve_status_edit(request, copy_pk, requirement_pk):
    """In-place editing of one copy's sleeved state for one sleeve
    requirement (DESIGN §5), optionally recording which product was used.
    Owner+active scoped like curation_edit; the status row is created on
    first edit (the worklist shows requirement slots that have no row yet)."""
    copy = get_object_or_404(
        Copy, pk=copy_pk, owner=request.user,
        archive_status=Copy.ArchiveStatus.ACTIVE,
    )
    requirement = get_object_or_404(SleeveRequirement, pk=requirement_pk)

    updates = {}
    if "status" in request.POST:
        value = request.POST["status"]
        if value not in CopySleeveStatus.Status.values:
            return HttpResponseBadRequest("Unknown sleeve status.")
        updates["status"] = value
    if "product" in request.POST:
        raw = request.POST["product"]
        if not raw:
            updates["product"] = None
        else:
            # Only a product of the right size can have been used.
            product = SleeveProduct.objects.filter(
                pk=_parse_int(raw), card_size=requirement.card_size,
            ).first()
            if product is None:
                return HttpResponseBadRequest("Unknown sleeve product for this size.")
            updates["product"] = product
    if updates:
        CopySleeveStatus.objects.update_or_create(
            copy=copy, requirement=requirement, defaults=updates,
        )

    # The same endpoint feeds two sites (issue #17): the copy edit card swaps
    # back just this copy's rows (scope=copy), the worklist the whole re-sorted
    # and re-filtered table.
    if request.POST.get("scope") == "copy":
        return render(request, "partials/sleeve_table.html",
                      _copy_sleeve_table_context(copy))
    return render(request, "partials/sleeve_status_table.html",
                  _sleeve_status_context(request.user, request.POST))


def _requirement_editor_context(edition):
    """Rows + size choices for the edition's sleeve-requirement editor
    (issue #129). SleeveRequirement has no Meta.ordering, so sort explicitly by
    the card size's dimensions, matching the rest of §5."""
    return {
        "edition": edition,
        "requirements": (
            edition.sleeve_requirements.select_related("card_size")
            .order_by("card_size__width_mm", "card_size__height_mm")
        ),
        "card_sizes": CardSize.objects.all(),
    }


@login_required
@require_POST
def requirement_add(request, pk):
    """Add (or re-set the count of) one sleeve requirement on an edition
    (DESIGN §5, issue #129). The card size is either an existing CardSize or one
    defined inline by width×height — identity is the dimension pair
    (unique_card_size_dimensions), so a new size get_or_creates on (w, h).
    update_or_create on (edition, card_size) keeps a re-add idempotent against
    unique_requirement_per_edition_size (it just updates the count)."""
    edition = get_object_or_404(Edition, pk=pk)

    count = _parse_int(request.POST.get("count", "").strip())
    if count is None or count < 1:
        return HttpResponseBadRequest("Count must be a whole number of at least 1.")

    choice = request.POST.get("card_size", "").strip()
    if choice and choice != "new":
        card_size = get_object_or_404(CardSize, pk=_parse_int(choice))
    else:
        width = _parse_decimal_mm(request.POST.get("width_mm", ""))
        height = _parse_decimal_mm(request.POST.get("height_mm", ""))
        if width is None or height is None:
            return HttpResponseBadRequest(
                "A new size needs positive width and height in millimetres.")
        card_size, _ = CardSize.objects.get_or_create(
            width_mm=width, height_mm=height,
            defaults={"name": request.POST.get("size_name", "").strip()},
        )

    SleeveRequirement.objects.update_or_create(
        edition=edition, card_size=card_size, defaults={"count": count},
    )
    return render(request, "partials/sleeve_requirements_editor.html",
                  _requirement_editor_context(edition))


@login_required
@require_POST
def requirement_edit(request, pk):
    """Edit the card count on one existing requirement row (issue #129)."""
    requirement = get_object_or_404(
        SleeveRequirement.objects.select_related("edition"), pk=pk)
    count = _parse_int(request.POST.get("count", "").strip())
    if count is None or count < 1:
        return HttpResponseBadRequest("Count must be a whole number of at least 1.")
    requirement.count = count
    requirement.save(update_fields=["count"])
    return render(request, "partials/sleeve_requirements_editor.html",
                  _requirement_editor_context(requirement.edition))


@login_required
@require_POST
def requirement_delete(request, pk):
    """Remove one sleeve requirement from its edition (issue #129)."""
    requirement = get_object_or_404(
        SleeveRequirement.objects.select_related("edition"), pk=pk)
    edition = requirement.edition
    requirement.delete()
    return render(request, "partials/sleeve_requirements_editor.html",
                  _requirement_editor_context(edition))


def _parse_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_decimal_mm(value):
    """A positive millimetre dimension for a CardSize, quantized to the model's
    single decimal place. None when blank or not a positive number."""
    try:
        mm = Decimal(str(value).strip())
    except (InvalidOperation, TypeError):
        return None
    if mm <= 0:
        return None
    return mm.quantize(Decimal("0.1"))


def _parse_weight(value):
    """A GameChooser weight-slider value, quantized to Game.weight's decimal
    places. None when blank or not a positive number."""
    try:
        weight = Decimal(str(value).strip())
    except (InvalidOperation, TypeError):
        return None
    if weight <= 0:
        return None
    return weight.quantize(Decimal("0.01"))


def _fold(text):
    """Case- and accent-blind form for name search (#126): NFKD-decompose,
    drop combining marks so 'Šelmy' folds to 'selmy', then lowercase. Applied
    symmetrically to the query and every haystack, so an unaccented query
    matches accented titles and vice-versa."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).lower()


def _active_copies(game):
    """A game's non-archived copies across all its editions. Prefetch
    "editions__copies" (or deeper) to avoid N+1."""
    return [
        copy
        for edition in game.editions.all()
        for copy in edition.copies.all()
        if copy.archive_status == Copy.ArchiveStatus.ACTIVE
    ]


def _game_matches(game, filters):
    """One game against the GameChooser axes (DESIGN §10). Games missing the
    data for an ACTIVE numeric filter are excluded — an unknown player count
    cannot promise to seat 5."""
    q = _fold(filters["q"])
    if q:
        # Match the game's own name/bgg_name, its alternate names (#51), and
        # its series name (#86) — so searching a series or localized title
        # surfaces the (collapsed) tile even when no primary name contains q.
        # Diacritics are folded on both sides (#126) so 'selmy' finds 'Šelmy'.
        haystacks = [_fold(game.name), _fold(game.bgg_name)]
        haystacks.extend(_fold(alt.name) for alt in game.alternate_names.all())
        if game.series_id:
            haystacks.append(_fold(game.series.name))
        if not any(q in haystack for haystack in haystacks):
            return False

    players = filters["players"]
    if players is not None:
        low, high = game.effective_player_range()
        if low is None and high is None:
            return False
        if (low is not None and players < low) or (high is not None and players > high):
            return False

    playtime = filters["playtime"]
    if playtime is not None:
        # "Could fit": the game's shortest advertised playtime fits the slot.
        shortest = game.min_playtime or game.max_playtime
        if not shortest or shortest > playtime:
            return False

    if filters["game_types"]:
        marked = {egt["game_type"] for egt in game.effective_game_types()}
        if not marked.intersection(filters["game_types"]):
            return False

    if filters["themes"] or filters["mechanics"]:
        tagged = {gt.tag_id for gt in game.game_tags.all()}
        if filters["themes"] and not tagged.intersection(filters["themes"]):
            return False
        if filters["mechanics"] and not tagged.intersection(filters["mechanics"]):
            return False

    weight_mode = filters["weight_mode"]
    if weight_mode:
        if game.weight is None:
            return False
        if weight_mode == "max" and game.weight > filters["weight"]:
            return False
        if weight_mode == "min" and game.weight < filters["weight"]:
            return False

    played = filters["played"]
    if played == "played" and not game.bgg_numplays:
        return False
    if played == "never" and game.bgg_numplays:
        return False

    if filters["families"]:
        joined = {family.pk for family in game.families.all()}
        if not joined.intersection(filters["families"]):
            return False

    copies = _active_copies(game)
    location = filters["location"]
    if location == "none":
        # Issue #30: unplaced copies, so they can be found and given a home.
        if not any(copy.location_id is None for copy in copies):
            return False
    elif location is not None:
        if not any(copy.location_id == location for copy in copies):
            return False

    if filters["available"]:
        # Available = an active, ready-to-play copy that is not lent out.
        # Issue #19: a not-yet-printed/prepped copy isn't actually playable.
        # Issue #43: lent-out is now tracked via Loan, not Location.
        if not any(
            copy.ready_status == Copy.ReadyStatus.READY
            and (copy.active_loan is None
                 or copy.active_loan.direction != Loan.Direction.LENT_OUT)
            for copy in copies
        ):
            return False

    if filters["leaving"]:
        # Issue #82: at least one active copy marked keep_status=WILL_LEAVE.
        if not any(copy.keep_status == Copy.KeepStatus.WILL_LEAVE for copy in copies):
            return False

    return True


def _build_filters(request, *, owner_axes=True):
    """The GameChooser axis values (DESIGN §10) from the query string. Two
    behaviours are DEFAULTS carried by an empty query string and only add a
    param when the user opts OUT (issue #107): availability (available-only
    unless ?show_unavailable) and series collapse (collapsed unless
    ?show_all_editions). owner_axes=False drops the owner-only axes — location
    and availability are the owner's shelf/lending state, never exposed to a
    viewer (issue #120, DESIGN §3), so they can neither leak nor be probed.
    """
    raw_location = request.GET.get("location")
    return {
        # Issue #20: name search, matched (case-blind) against the
        # user-facing name, the BGG canonical one, curated alternate names
        # (#51) and the series name (#86). Kept as typed so the box echoes
        # it back.
        "q": request.GET.get("q", "").strip(),
        "players": _parse_int(request.GET.get("players")),
        "playtime": _parse_int(request.GET.get("playtime")),
        "game_types": set(request.GET.getlist("game_type")),
        "themes": {t for t in map(_parse_int, request.GET.getlist("theme")) if t is not None},
        # Issue #63: mechanic facet — pk-selected like themes, same tag kind
        # split by Tag.kind rather than a separate model.
        "mechanics": {m for m in map(_parse_int, request.GET.getlist("mechanic")) if m is not None},
        # Issue #147: single-value slider whose direction is picked by
        # weight_mode ("max"/"min"); unset or any other value means "any"
        # (filter inactive) so a stray query param can't accidentally filter.
        "weight": _parse_weight(request.GET.get("weight")),
        "weight_mode": request.GET.get("weight_mode") if request.GET.get("weight_mode") in ("max", "min") else None,
        # Issue #63: played vs never-played, fed by bgg_numplays (already
        # token-free). "" / unset / anything else means "any".
        "played": request.GET.get("played") if request.GET.get("played") in ("played", "never") else None,
        # Issue #42: family facet — pk-selected like themes.
        "families": {f for f in map(_parse_int, request.GET.getlist("family")) if f is not None},
        "expansions": bool(request.GET.get("expansions")),
        # Issue #107 opt-out default: series collapse is on with a clean URL.
        "collapse": not request.GET.get("show_all_editions"),
        # "none" = copies with no location set (issue #30). Owner-only.
        "location": ("none" if raw_location == "none" else _parse_int(raw_location))
        if owner_axes else None,
        # Issue #107 opt-out default: available-only with a clean URL. Owner-only
        # (issue #120): viewers never filter by lending state, so it's forced off.
        "available": (not request.GET.get("show_unavailable")) if owner_axes else False,
        # Issue #82: opt-in axis, only active copies marked WILL_LEAVE.
        # Owner-only — same personal-curation rationale as location/available.
        "leaving": bool(request.GET.get("leaving")) if owner_axes else False,
        # Issue #92: display-only axis (grid vs. list) — never consulted by
        # _game_matches, just carried alongside the real filters so the
        # template and hx-push-url'd URL agree on which partial to render.
        "view": "list" if request.GET.get("view") == "list" else "grid",
        # Issue #92 follow-up: which list-view column to sort by, "-"
        # prefixed for descending. Only consulted when view == "list";
        # falls back to the name order every tile already carries.
        "sort": request.GET.get("sort") or "name",
    }


def _collapse_into_tiles(matching, incoming_pks, *, collapse, url_key,
                         detail_url_name, series_url_name, leaving_pks=frozenset()):
    """Turn matched games into grid tiles. With collapse=True a series shows as
    one tile (issue #21) whose overlay lists ALL members regardless of the
    filters — the series is the navigation unit; a match by ANY member vouches
    for the tile. With it off ('show all editions', issue #107) every game is
    its own tile. Detail/series URLs are reversed here and stapled to each tile
    so the grid template needs no {% url %} arg-count branching between the owner
    grid ('game_detail' by pk) and the shared grids (by url_key + pk). A None
    series_url_name means no series page exists for this surface (the shared
    views): the tile still collapses but only expands to its members.

    leaving_pks (issue #82) mirrors incoming_pks — an owner-only signal, so
    the shared/public grid call site leaves it at the default empty set.
    """
    def detail_url(pk):
        return reverse(detail_url_name, args=[pk] if url_key is None else [url_key, pk])

    tiles, series_seen = [], set()
    for game in matching:
        if collapse and game.series_id:
            if game.series_id in series_seen:
                continue
            series_seen.add(game.series_id)
            series = game.series
            members = list(series.members.all())  # Meta-ordered (sort_name)
            for member in members:
                member.is_incoming = member.pk in incoming_pks
                member.is_leaving = member.pk in leaving_pks
                member.detail_url = detail_url(member.pk)
            # Issue #105: a shared status rolls up to the collapsed tile only
            # when EVERY member matches (unlike the is_incoming/is_leaving
            # any-match above) — there's no "mixed" status to fall back on.
            statuses = {m.bgg_collection_status for m in members}
            tiles.append({
                "series": series,
                "members": members,
                "cover_source": series.cover_source,
                "is_incoming": any(m.is_incoming for m in members),
                "is_leaving": any(m.is_leaving for m in members),
                "shared_status": statuses.pop() if len(statuses) == 1 else "",
                "series_url": reverse(series_url_name, args=[series.pk])
                if series_url_name else None,
                "sort_key": Game.compute_sort_name(series.name),
            })
        else:
            tiles.append({"game": game, "cover_source": game,
                          "detail_url": detail_url(game.pk),
                          "sort_key": game.sort_name})
    # A series tile files under the SERIES name (article-blind like game
    # sort_name, issue #6), which may sit elsewhere than its members did.
    tiles.sort(key=lambda tile: tile["sort_key"])
    return tiles


def _copy_summary(game):
    """(count, location_label, keep_label) over a game's active copies
    (issue #92 list view). Location/keep-status show the shared value when
    every active copy agrees, else "multiple"/"mixed" — a single row can't
    show one game's several copies individually."""
    copies = _active_copies(game)
    if not copies:
        return 0, "—", "—"

    locations = {copy.location_id for copy in copies}
    if len(locations) > 1:
        location_label = "multiple"
    else:
        location = copies[0].location
        location_label = location.name if location else "no location set"

    keep_statuses = {copy.keep_status for copy in copies}
    if len(keep_statuses) > 1:
        keep_label = "mixed"
    else:
        keep_label = copies[0].get_keep_status_display() or "—"

    return len(copies), location_label, keep_label


def _annotate_list_row(tile):
    """Attach list-view row data to a _collapse_into_tiles tile in place
    (issue #92). A series row reports its primary_game's stats — the same
    representative-game concept the grid already uses for the cover — so
    the row tells one coherent story instead of mixing per-member values."""
    game = tile["series"].primary_game if "series" in tile else tile["game"]
    tile["stat_game"] = game
    tile["players_low"], tile["players_high"] = game.effective_player_range()
    tile["copy_count"], tile["location_label"], tile["keep_label"] = _copy_summary(game)


# Issue #92 follow-up: sortable list-view columns. Order here IS the column
# order in game_list.html's <thead>/<tbody> — both are built by looping this
# same list, so header and cells always line up. Owner-only columns are
# appended separately, gated the same way the cells themselves are.
_SORT_COLUMNS = [
    ("name", "Name"), ("type", "Type"), ("year", "Year"),
    ("players", "Players"), ("playtime", "Playtime"), ("weight", "Weight"),
    ("rank", "BGG rank"),
]
_OWNER_SORT_COLUMNS = [
    ("location", "Location"), ("keep", "Keep/leave"), ("copies", "Copies"),
]
_SORT_KEY_FUNCS = {
    "name": lambda tile: tile["sort_key"],
    "type": lambda tile: tile["stat_game"].get_type_display(),
    "year": lambda tile: tile["stat_game"].year_published,
    "players": lambda tile: tile["players_low"],
    "playtime": lambda tile: tile["stat_game"].min_playtime,
    "weight": lambda tile: tile["stat_game"].weight,
    "rank": lambda tile: tile["stat_game"].bgg_rank,
    "location": lambda tile: tile["location_label"],
    "keep": lambda tile: tile["keep_label"],
    "copies": lambda tile: tile["copy_count"],
}


def _sort_tiles_for_list(tiles, sort):
    """Reorder annotated tiles by a "col" / "-col" sort spec from the query
    string (issue #92 follow-up). Tiles missing the sorted value (e.g. no
    year_published) sort last regardless of direction — pushing them to the
    top on a descending sort would bury every game that actually has data."""
    column = sort[1:] if sort.startswith("-") else sort
    descending = sort.startswith("-")
    key_func = _SORT_KEY_FUNCS.get(column, _SORT_KEY_FUNCS["name"])
    present = [tile for tile in tiles if key_func(tile) is not None]
    missing = [tile for tile in tiles if key_func(tile) is None]
    present.sort(key=key_func, reverse=descending)
    return present + missing


def _sort_column_context(sort, *, owner_view):
    """Per-column header state for game_list.html: the label, the sort spec
    a click on this header should request next (toggling asc/desc on the
    active column, defaulting to ascending on any other), and the arrow to
    show now."""
    columns = _SORT_COLUMNS + (_OWNER_SORT_COLUMNS if owner_view else [])
    active_column = sort[1:] if sort.startswith("-") else sort
    active_descending = sort.startswith("-")
    context = []
    for key, label in columns:
        active = key == active_column
        is_descending = active and active_descending
        context.append({
            "key": key,
            "label": label,
            "next_sort": key if is_descending else (f"-{key}" if active else key),
            "direction": "desc" if is_descending else ("asc" if active else None),
        })
    return context


@login_required
def collection(request):
    """The GameChooser (DESIGN §10) on the cover-art browse grid (§13): live
    htmx filtering by players (effective), playtime, game-type, theme,
    mechanic, weight, played-status, location and availability.

    ~330 games filter comfortably in Python; effective player ranges (base
    stats widened by owned expansions' overrides) don't fit a queryset
    anyway.
    """
    filters = _build_filters(request, owner_axes=True)

    games = Game.objects.select_related("series__primary_game").prefetch_related(
        "expansions__editions__copies", "expansions__game_types",
        "game_types", "game_tags",
        "editions__copies__location", "editions__copies__loans",
        "series__members", "families", "alternate_names",
    )
    # Base games only by default; owned expansions still widen their base's
    # effective player range via the prefetch above.
    if not filters["expansions"]:
        games = games.filter(type=Game.Type.BASE)
    matching = [game for game in games if _game_matches(game, filters)]

    # Issue #8: flag games with an unconverted preorder Product (the copy
    # hasn't arrived/been converted yet) so the grid can mark them incoming.
    # Owner-scoped like the game_detail backlink (§6) — a personal signal, so
    # it never leaks onto the public share grid (that uses share_collection).
    incoming_game_pks = set(
        Product.objects.filter(
            game__isnull=False, copy__isnull=True,
            wave__purchase__owner=request.user,
        ).values_list("game_id", flat=True)
    )
    # Issue #82: same rationale as incoming_game_pks — owner-only, so the
    # grid can mark games with an active WILL_LEAVE copy.
    leaving_game_pks = set(
        Copy.objects.filter(
            owner=request.user, keep_status=Copy.KeepStatus.WILL_LEAVE,
            archive_status=Copy.ArchiveStatus.ACTIVE, is_borrowed_in=False,
        ).values_list("edition__game_id", flat=True)
    )
    for game in matching:
        game.is_incoming = game.pk in incoming_game_pks
        game.is_leaving = game.pk in leaving_game_pks

    tiles = _collapse_into_tiles(
        matching, incoming_game_pks, collapse=filters["collapse"],
        url_key=None, detail_url_name="game_detail",
        series_url_name="series_detail", leaving_pks=leaving_game_pks,
    )
    sort_columns = []
    if filters["view"] == "list":
        for tile in tiles:
            _annotate_list_row(tile)
        tiles = _sort_tiles_for_list(tiles, filters["sort"])
        sort_columns = _sort_column_context(filters["sort"], owner_view=True)

    context = {
        "games": matching,
        "tiles": tiles,
        # Header counts stay in GAMES, not tiles — a collapsed series
        # legitimately shows fewer tiles than matched games.
        "game_count": len(matching),
        "total_count": games.count(),
        "filters": filters,
        "owner_view": True,
        "chooser_url": reverse("collection"),
        "reset_url": reverse("collection"),
        "game_type_choices": GameType.Type.choices,
        "theme_tags": Tag.objects.filter(kind=Tag.Kind.THEME),
        "mechanic_tags": Tag.objects.filter(kind=Tag.Kind.MECHANIC),
        "family_choices": Family.objects.all(),
        "locations": Location.objects.all(),
        "sort_columns": sort_columns,
    }
    if request.headers.get("HX-Request"):
        return render(request, "partials/game_results.html", context)
    return render(request, "collection.html", context)


def _cull_priority(copy):
    """DESIGN §11 cull order: lowest excitement first (it replaces rating and
    is the primary signal; unrated copies sink — no signal to judge by), a
    filled-in why-it-might-leave beats a blank one, then name."""
    return (
        copy.excitement is None,
        copy.excitement if copy.excitement is not None else 0,
        not copy.why_might_leave,
        copy.edition.game.name,
    )


# Issue #40: user-driven sorting for the curation table, independent of the
# _cull_priority default above. Order here IS the column order in
# cull_table.html's <thead> — the template loops this list for its first
# four headers, same idiom as _SORT_COLUMNS/game_list.html.
_CURATION_SORT_COLUMNS = [
    ("name", "Game"), ("last_played", "Last played"),
    ("excitement", "Excitement"), ("keep", "Keep"),
]
_CURATION_SORT_KEY_FUNCS = {
    "name": lambda copy: copy.edition.game.name,
    "excitement": lambda copy: copy.excitement,
    "keep": lambda copy: copy.get_keep_status_display() or None,
    "last_played": lambda copy: copy.last_played,
}


def _sort_copies_for_curation(copies, sort):
    """Reorder curation copies by a "col"/"-col" sort spec from the query
    string (issue #40). Missing values sink to the bottom regardless of
    direction, same as the collection list's _sort_tiles_for_list — except
    last_played, where "never played" is the most cull-relevant state, not a
    data gap: it's treated as infinitely long ago via a date.min sentinel, so
    it sorts first on ascending (oldest-first) and last on descending."""
    column = sort[1:] if sort.startswith("-") else sort
    descending = sort.startswith("-")
    key_func = _CURATION_SORT_KEY_FUNCS.get(column)
    if key_func is None:
        return sorted(copies, key=_cull_priority)
    if column == "last_played":
        return sorted(
            copies, key=lambda c: key_func(c) or datetime.date.min, reverse=descending,
        )
    present = [c for c in copies if key_func(c) is not None]
    missing = [c for c in copies if key_func(c) is None]
    present.sort(key=key_func, reverse=descending)
    return present + missing


def _curation_sort_column_context(sort):
    """Per-column header state for cull_table.html: label, the sort spec a
    click on this header should request next, and the arrow to show now.
    None of the columns match an empty sort, so no header shows as active
    when the table is at its default cull-priority order."""
    active_column = sort[1:] if sort.startswith("-") else sort
    active_descending = sort.startswith("-")
    context = []
    for key, label in _CURATION_SORT_COLUMNS:
        active = key == active_column
        is_descending = active and active_descending
        context.append({
            "key": key,
            "label": label,
            "next_sort": key if is_descending else (f"-{key}" if active else key),
            "direction": "desc" if is_descending else ("asc" if active else None),
        })
    return context


def _curation_context(user, data, frozen_order=None):
    """Cull table context for the given filter params (a GET or POST
    QueryDict — the edit endpoint hx-include's the filter form into its POST
    body so the re-rendered table keeps the caller's filters).

    Issue #24: an inline edit resorts the table using the very fields it
    just changed, so the edited row can jump elsewhere. frozen_order (a list
    of Copy pks) pins the table to that prior display order instead of a
    fresh cull-priority sort; unknown pks fall back to cull-priority among
    themselves, after the pinned ones. The response always re-emits the
    order it actually used as frozen_order, so it survives any number of
    edits and only resets on a real page load or filter change (neither of
    which posts frozen_order)."""
    # The include-immune toggle is named show_immune (not immune) so the
    # hx-include'd filter form can never collide with the row checkboxes'
    # immune edit param.
    filters = {
        "show_immune": bool(data.get("show_immune")),
        "keep": data.get("keep") or "",
        "show_expansions": bool(data.get("show_expansions")),
    }
    # Issue #40: a clicked column header, empty by default (cull-priority).
    sort = data.get("sort") or ""

    # Issue #43: you can't cull what you don't own — a borrowed-in copy
    # never appears among cull candidates.
    copies = Copy.objects.filter(
        owner=user, archive_status=Copy.ArchiveStatus.ACTIVE, is_borrowed_in=False,
    ).select_related("edition__game", "location").annotate(
        last_played=Max("edition__game__plays__play_date"),
    )
    total_count = copies.count()
    if not filters["show_immune"]:
        copies = copies.filter(immune=False)
    if filters["keep"]:
        copies = copies.filter(keep_status=filters["keep"])
    # Issue #39: expansions clutter the cull list — hide them by default,
    # same opt-in toggle idiom as show_immune above.
    if not filters["show_expansions"]:
        copies = copies.filter(edition__game__type=Game.Type.BASE)

    if frozen_order:
        order_index = {pk: i for i, pk in enumerate(frozen_order)}
        copies = sorted(
            copies,
            key=lambda c: (order_index.get(c.pk, len(frozen_order)), _cull_priority(c)),
        )
    elif sort:
        copies = _sort_copies_for_curation(copies, sort)
    else:
        copies = sorted(copies, key=_cull_priority)

    # Issue #40: view-computed day delta, same convention as
    # _ending_soon_rows's days_left — the template just renders the number.
    today = timezone.localdate()
    for copy in copies:
        copy.last_played_days_ago = (
            (today - copy.last_played).days if copy.last_played else None
        )

    return {
        "copies": copies,
        "copy_count": len(copies),
        "total_count": total_count,
        "filters": filters,
        "keep_status_choices": Copy.KeepStatus.choices,
        "frozen_order": ",".join(str(c.pk) for c in copies),
        "sort": sort,
        "sort_columns": _curation_sort_column_context(sort),
    }


@login_required
def curation(request):
    """The cull-candidates view (DESIGN §11): the owner's active copies in
    cull-priority order. Immune copies are out of the running by definition,
    so they hide by default behind a toggle; keep-status narrows further.
    Filters re-render the table via htmx, bookmarkable with hx-push-url.

    Curation fields are personal (per-Copy, per-owner), so unlike the
    collection grid this only ever shows request.user's copies.
    """
    context = _curation_context(request.user, request.GET)
    if request.headers.get("HX-Request"):
        return render(request, "partials/cull_table.html", context)
    return render(request, "curation.html", context)


@login_required
@require_POST
def curation_edit(request, pk):
    """In-place editing of the §11 cull signals (excitement, keep-status,
    immune, why-it-might-leave) straight from the curation
    table. Owner-scoped like the table itself — someone else's (or an
    archived) copy is a 404. Returns the whole re-rendered table with the
    filters re-applied immediately; the row order stays pinned to whatever
    it was before this edit (issue #24, via the posted frozen_order) so
    editing a row's own sort fields doesn't make it jump —
    simple-history logs the change like an admin edit would."""
    copy = get_object_or_404(
        Copy, pk=pk, owner=request.user,
        archive_status=Copy.ArchiveStatus.ACTIVE, is_borrowed_in=False,
    )

    updated = []
    if "excitement" in request.POST:
        raw = request.POST["excitement"].strip()
        if not raw:
            copy.excitement = None
        else:
            try:
                value = Decimal(raw)
            except InvalidOperation:
                return HttpResponseBadRequest("Excitement must be a number.")
            if not 0 <= value <= 10:
                return HttpResponseBadRequest("Excitement is 0–10.")
            copy.excitement = value.quantize(Decimal("0.1"))
        updated.append("excitement")
    if "keep_status" in request.POST:
        value = request.POST["keep_status"]
        if value and value not in Copy.KeepStatus.values:
            return HttpResponseBadRequest("Unknown keep status.")
        copy.keep_status = value
        updated.append("keep_status")
    if "immune" in request.POST:
        # The row checkbox always posts an explicit "1"/"0" via hx-vals —
        # an unchecked checkbox alone would send nothing at all.
        value = request.POST["immune"]
        if value not in ("0", "1"):
            return HttpResponseBadRequest("Immune must be 0 or 1.")
        copy.immune = value == "1"
        updated.append("immune")
    if "why_might_leave" in request.POST:
        copy.why_might_leave = request.POST["why_might_leave"].strip()
        updated.append("why_might_leave")
    if updated:
        copy.save(update_fields=updated + ["updated_at"])
    if "keep_status" in updated:
        _sync_leaving_status(copy.edition.game, request.user)

    frozen_order = [
        int(pk) for pk in request.POST.get("frozen_order", "").split(",")
        if pk.strip().isdigit()
    ]
    return render(request, "partials/cull_table.html",
                  _curation_context(request.user, request.POST, frozen_order))


@login_required
@require_POST
def curation_archive(request, pk):
    """The §11 "cull this" endpoint of the curation lifecycle: archive the
    copy (DESIGN §4 — retained for reference, hidden from active views).
    Reason defaults to culled, the reason this table exists; the others
    (sold/gifted/lost) are accepted for completeness. Same owner/active 404
    scoping and whole-table swap as curation_edit — the archived row simply
    drops out of the re-rendered table."""
    copy = get_object_or_404(
        Copy, pk=pk, owner=request.user,
        archive_status=Copy.ArchiveStatus.ACTIVE, is_borrowed_in=False,
    )
    reason = request.POST.get("reason") or Copy.ArchiveReason.CULLED
    if reason not in Copy.ArchiveReason.values:
        return HttpResponseBadRequest("Unknown archive reason.")
    copy.archive_status = Copy.ArchiveStatus.ARCHIVED
    copy.archive_reason = reason
    copy.archive_date = timezone.localdate()
    copy.save(update_fields=[
        "archive_status", "archive_reason", "archive_date", "updated_at",
    ])

    # Issue #117: "previously owned" only if no OTHER active, truly-owned
    # copy of this game remains (a different edition can still be owned —
    # rebuy/upgrade; a borrowed-in copy, issue #43, doesn't count as owned).
    game = copy.edition.game
    if not Copy.objects.filter(
        owner=request.user, edition__game=game,
        archive_status=Copy.ArchiveStatus.ACTIVE, is_borrowed_in=False,
    ).exists():
        _enqueue_bgg_push(game, Game.BggCollectionStatus.PREV_OWNED, request.user)
    # Issue #82: the archived copy may have been the one carrying
    # WILL_LEAVE — recompute regardless of the branch above.
    _sync_leaving_status(game, request.user)

    return render(request, "partials/cull_table.html",
                  _curation_context(request.user, request.POST))


def _archived_context(user, data):
    filters = {"reason": data.get("reason") or ""}
    copies = Copy.objects.filter(
        owner=user, archive_status=Copy.ArchiveStatus.ARCHIVED,
    ).select_related("edition__game")
    total_count = copies.count()
    if filters["reason"] in Copy.ArchiveReason.values:
        copies = copies.filter(archive_reason=filters["reason"])
    copies = copies.order_by(
        F("archive_date").desc(nulls_last=True), "edition__game__name",
    )
    return {
        "copies": copies,
        "copy_count": copies.count(),
        "total_count": total_count,
        "filters": filters,
        "reason_choices": Copy.ArchiveReason.choices,
    }


@login_required
def archived_copies(request):
    """The §4 archive shelf: copies that left the active collection —
    "retained for reference, hidden from active views, still findable".
    Newest departures first; the Cull button on the curation table is what
    feeds this. Browse-only and owner-scoped like the curation view (the
    cull signals frozen on these rows are personal); un-archiving stays an
    admin action. Same htmx filter rig as everywhere else."""
    context = _archived_context(request.user, request.GET)
    if request.headers.get("HX-Request"):
        return render(request, "partials/archive_table.html", context)
    return render(request, "archive.html", context)


# ---------------------------------------------------------------------------
# DESIGN §6 purchases / crowdfunding: the user-facing pipeline browse view.
# Owner-scoped like the dashboard widgets — purchases are personal (§6), and
# the browse list surfaces addresses/pledge details the group has no business
# seeing yet.
# ---------------------------------------------------------------------------

# Browse order: what you are waiting on first, settled purchases last.
PURCHASE_STAGE_ORDER = {
    Purchase.Status.COMMITTED: 0,
    Purchase.Status.PLACEHOLDER: 1,
    Purchase.Status.WATCHING: 2,
    Purchase.Status.PASSED: 3,
    Purchase.Status.REFUNDED: 4,
    Purchase.Status.NEVER_DELIVERED: 5,
}

# "Settled" purchases — nothing left to do — sink below the active ones (#36):
# a fully fulfilled one arrived in full, and passed/refunded/never-delivered are
# dead ends. A placeholder still needs a decision, so it outranks a fulfilled buy.
PURCHASE_DONE_STATUSES = frozenset({
    Purchase.Status.PASSED,
    Purchase.Status.REFUNDED,
    Purchase.Status.NEVER_DELIVERED,
})


def _purchase_rows(purchases):
    """Per-purchase summary lines for the browse table: wave progress, the
    soonest ETA still on the way and the derived fulfilled flag (§6)."""
    rows = []
    for purchase in purchases:
        waves = list(purchase.waves.all())
        incoming = [w for w in waves if w.status not in Wave.TERMINAL_STATUSES]
        etas = [w.expected_arrival or w.original_eta for w in incoming]
        fulfilled = purchase.is_fulfilled
        rows.append({
            "purchase": purchase,
            "product_count": sum(len(w.products.all()) for w in waves),
            "waves_total": len(waves),
            "waves_arrived": sum(
                1 for w in waves if w.status == Wave.Status.ARRIVED
            ),
            "next_eta": min((eta for eta in etas if eta), default=None),
            "fulfilled": fulfilled,
            # Nothing is arriving for a placeholder ($1 hold) or a passed buy,
            # so wave progress there is noise (#94).
            "hide_waves": purchase.status in (
                Purchase.Status.PLACEHOLDER, Purchase.Status.PASSED,
            ),
            # The PM close date only matters while a decision/delivery pends —
            # not once fulfilled or passed (#36).
            "hide_close_date": fulfilled or purchase.status == Purchase.Status.PASSED,
        })
    rows.sort(key=lambda row: (
        # Settled (done) purchases sink below the active ones (#36).
        row["fulfilled"] or row["purchase"].status in PURCHASE_DONE_STATUSES,
        PURCHASE_STAGE_ORDER.get(row["purchase"].status, len(PURCHASE_STAGE_ORDER)),
        row["next_eta"] or datetime.date.max,
        row["purchase"].name.lower(),
    ))
    return rows


def _purchases_context(user, data):
    filters = {
        "status": data.get("status") or "",
        "platform": data.get("platform") or "",
        "q": (data.get("q") or "").strip(),
    }
    purchases = Purchase.objects.filter(owner=user).prefetch_related(
        "waves__products",
    )
    total_count = purchases.count()
    if filters["status"] in Purchase.Status.values:
        purchases = purchases.filter(status=filters["status"])
    if filters["platform"] in Purchase.Platform.values:
        purchases = purchases.filter(platform=filters["platform"])
    q = _fold(filters["q"])
    if q:
        # Fold-and-substring name search (#151), same approach as the
        # GameChooser's _game_matches (#126): case- and accent-blind.
        purchases = [p for p in purchases if q in _fold(p.name)]

    rows = _purchase_rows(purchases)
    return {
        "rows": rows,
        "purchase_count": len(rows),
        "total_count": total_count,
        "filters": filters,
        "status_choices": Purchase.Status.choices,
        "platform_choices": Purchase.Platform.choices,
    }


@login_required
def purchases(request):
    """The §6 purchase-pipeline browse view: every campaign/preorder with
    its lifecycle status and wave progress, filterable by status and
    platform. Same htmx rig as the curation table — filters swap the table,
    bookmarkable with hx-push-url. The dashboard stays the "needs
    attention" cut; this is the whole list."""
    context = _purchases_context(request.user, request.GET)
    if request.headers.get("HX-Request"):
        return render(request, "partials/purchase_table.html", context)
    return render(request, "purchases.html", context)


@login_required
def purchase_detail(request, pk):
    """One purchase with its full §6 hierarchy: campaign/pledge-manager
    facts, then every wave with dates, derived delay, tracking and its
    products (game products link into the collection)."""
    purchase = get_object_or_404(
        # Editions feed the convert-to-copy select on arrived game products.
        Purchase.objects.prefetch_related("waves__products__game__editions"),
        pk=pk, owner=request.user,
    )
    return render(request, "purchase_detail.html", {
        "purchase": purchase,
        "waves": purchase.waves.all(),
        "fulfilled": purchase.is_fulfilled,
        "pledge_plan": getattr(purchase, "pledge_plan", None),
    })


# ---------------------------------------------------------------------------
# DESIGN §6 purchase editing (issue #5): user-facing add/edit for purchases,
# waves and products, plus the product→Copy conversion on arrival. Same
# settled-page style as copy_edit/game_edit — whole-form POSTs and redirects,
# owner-scoped 404s, 400s for values the form's own inputs already constrain.
# ---------------------------------------------------------------------------

def _parse_form_date(post, field):
    raw = post.get(field, "").strip()
    if not raw:
        return None
    value = parse_date(raw)
    if value is None:
        raise ValueError(f"{field} must be YYYY-MM-DD.")
    return value


def _purchase_edit_context(purchase, purchase_form, wave_formset, **extra):
    return {
        "purchase": purchase,
        "purchase_form": purchase_form,
        "wave_formset": wave_formset,
        "kind_choices": Product.Kind.choices,
        **extra,
    }


@login_required
def purchase_add(request):
    """Create a purchase (issue #5). GET renders the campaign form; POST
    creates the purchase plus its §6 auto "Wave 1" and continues on the
    edit page, where waves and products get filled in. A duplicate name is
    the one mistake the form can't prevent, so it re-renders inline with
    the typed values kept (sharing-settings pattern)."""
    purchase = Purchase(owner=request.user)
    if request.method != "POST":
        return render(request, "purchase_add.html", {
            "purchase": purchase,
            "purchase_form": PurchaseForm(instance=purchase, owner=request.user),
        })
    purchase_form = PurchaseForm(request.POST, instance=purchase, owner=request.user)
    if not purchase_form.is_valid():
        return render(request, "purchase_add.html", {
            "purchase": purchase, "purchase_form": purchase_form,
        })
    purchase = purchase_form.save()
    Wave.objects.create(purchase=purchase, number=1)
    return redirect("purchase_edit", pk=purchase.pk)


@login_required
def purchase_edit(request, pk):
    """The purchase edit page (issue #136): one save for the campaign form
    and every wave (status, dates, address, tracking) together — an
    inlineformset_factory formset, so editing several sections and saving
    once can no longer silently drop one of them. Wave/product deletion
    ride along as DELETE checkboxes on the same submit; adding a wave or a
    product stays its own small immediate action (wave_add, product_add)
    since neither risks losing an in-progress edit elsewhere on the page."""
    purchase = get_object_or_404(Purchase, pk=pk, owner=request.user)
    waves = purchase.waves.prefetch_related("products")
    if request.method != "POST":
        purchase_form = PurchaseForm(
            instance=purchase, owner=request.user, bind_to_edit_form=True)
        wave_formset = WaveFormSet(instance=purchase, queryset=waves)
        return render(request, "purchase_edit.html",
                      _purchase_edit_context(purchase, purchase_form, wave_formset))

    purchase_form = PurchaseForm(
        request.POST, instance=purchase, owner=request.user, bind_to_edit_form=True)
    wave_formset = WaveFormSet(request.POST, instance=purchase, queryset=waves)
    delete_product_pks = {
        int(raw_pk) for raw_pk in request.POST.getlist("delete_products")
        if raw_pk.isdigit()
    }
    if purchase_form.is_valid() and wave_formset.is_valid():
        with transaction.atomic():
            # Issue #166: captured before the formset save, since a deleted
            # wave cascades to its products (Product.wave is on_delete=CASCADE)
            # and would otherwise drop that game before it can be rechecked.
            game_ids = set(
                Product.objects.filter(wave__purchase=purchase, game__isnull=False)
                .values_list("game_id", flat=True)
            )
            purchase_form.save()
            wave_formset.save()
            Product.objects.filter(
                wave__purchase=purchase, copy__isnull=True, pk__in=delete_product_pks,
            ).delete()
        for game in Game.objects.filter(pk__in=game_ids):
            _sync_preorder_status(game, request.user)
        return redirect("purchase_detail", pk=purchase.pk)
    return render(request, "purchase_edit.html",
                  _purchase_edit_context(purchase, purchase_form, wave_formset))


@login_required
@require_POST
def wave_add(request, pk):
    """Add the next wave to a purchase — campaigns that ship in parts
    (§6). Numbering is automatic and gaps from deleted waves are not
    reused; waves are never renumbered."""
    purchase = get_object_or_404(Purchase, pk=pk, owner=request.user)
    last = purchase.waves.aggregate(last=Max("number"))["last"] or 0
    Wave.objects.create(purchase=purchase, number=last + 1)
    return redirect("purchase_edit", pk=purchase.pk)


@login_required
@require_POST
def product_add(request, pk):
    """Create a product on a wave from the name+kind mini-form on the
    purchase edit page, then jump to its edit page for the rest — the
    copy_add → copy_edit pattern."""
    wave = get_object_or_404(Wave, pk=pk, purchase__owner=request.user)
    name = request.POST.get("name", "").strip()
    if not name:
        return HttpResponseBadRequest("Name is required.")
    kind = request.POST.get("kind", Product.Kind.OTHER)
    if kind not in Product.Kind.values:
        return HttpResponseBadRequest("Unknown kind.")
    if wave.products.filter(name=name).exists():
        return HttpResponseBadRequest(
            "This wave already has an item with that name.")
    product = Product.objects.create(wave=wave, name=name, kind=kind)
    return redirect("product_edit", pk=product.pk)


def _product_edit_context(product, **extra):
    return {
        "product": product,
        "purchase": product.wave.purchase,
        **extra,
    }


@login_required
def product_detail(request, pk):
    """Read-only item page (issue #38): what a wave line-item is, with links
    to the game it delivers and the purchase it belongs to. Editing happens
    on product_edit, linked from the header."""
    product = get_object_or_404(
        Product.objects.select_related("wave__purchase", "game", "edition",
                                       "copy"),
        pk=pk, wave__purchase__owner=request.user,
    )
    return render(request, "product_detail.html", {
        "product": product,
        "purchase": product.wave.purchase,
    })


@login_required
def product_edit(request, pk):
    """The product edit page (issue #5): what a wave line-item is — kind,
    the Game/Edition it delivers, reference links and the provisional §6
    contents fields. The edition select always trails the *posted* game
    (its options come from it), so an edition that doesn't belong to the
    posted game is cleared rather than rejected — changing the game means
    save, then pick (ProductForm.clean, issue #28). Sleeve counts
    (ProductSleeveRequirement) stay admin-managed."""
    product = get_object_or_404(
        Product.objects.select_related("wave__purchase", "game"),
        pk=pk, wave__purchase__owner=request.user,
    )
    if request.method != "POST":
        return render(request, "product_edit.html", _product_edit_context(
            product, product_form=ProductForm(instance=product)))

    # Captured before the form binds — is_valid() mutates `product` in
    # place via construct_instance(), so this must not be read after that.
    old_game = product.game

    form = ProductForm(request.POST, instance=product)
    if not form.is_valid():
        return render(request, "product_edit.html",
                      _product_edit_context(product, product_form=form))
    product = form.save()
    # Issue #166: recompute for both the old and new game link, since
    # switching a product's game can drop one game's last incoming source
    # while adding a new one.
    for changed_game in {old_game, product.game} - {None}:
        _sync_preorder_status(changed_game, request.user)
    return redirect("purchase_edit", pk=product.wave.purchase_id)


@login_required
@require_POST
def product_convert(request, pk):
    """The §6 arrival seam: turn an arrived game product into a Copy and
    land on the copy's edit page. The edition comes from the product, the
    detail-page select, or is the game's default edition created on the
    fly (copy_add-style, for edition-less games). An existing ACTIVE copy
    of that edition gets linked instead of erroring — the common case for
    games already added to the collection by hand (the prev-owned
    backfill); an archived one is the §4 rebuy stance: a rebuy needs a
    new Edition row (admin), so it 400s. The product's provisional data
    moves onto the new copy: arrival date, the 3D-insert plan, and its
    sleeve counts become Edition requirements (they stop counting in the
    §5 preorder shortfall the moment the wave arrives)."""
    product = get_object_or_404(
        Product.objects.select_related("wave__purchase", "game", "edition"),
        pk=pk, wave__purchase__owner=request.user,
    )
    if product.copy_id:
        return HttpResponseBadRequest("Already converted.")
    if not product.game_id:
        return HttpResponseBadRequest(
            "Only products linked to a game can become a copy.")
    if product.wave.status != Wave.Status.ARRIVED:
        return HttpResponseBadRequest("The wave has not arrived yet.")

    game = product.game
    edition = product.edition
    if edition is None:
        edition_pk = request.POST.get("edition", "")
        if edition_pk:
            edition = (game.editions.filter(pk=edition_pk).first()
                       if edition_pk.isdigit() else None)
            if edition is None:
                return HttpResponseBadRequest("Unknown edition.")
        elif game.editions.exists():
            return HttpResponseBadRequest("Pick an edition.")
        else:
            edition = Edition.objects.create(game=game, is_default=True)

    copy = Copy.objects.filter(owner=request.user, edition=edition).first()
    if copy is not None and copy.archive_status != Copy.ArchiveStatus.ACTIVE:
        return HttpResponseBadRequest(
            "Your copy of this edition is archived — a rebuy needs a new "
            "edition (admin).")
    if copy is None:
        copy = Copy.objects.create(
            owner=request.user, edition=edition,
            acquired_date=product.wave.arrived_date or timezone.localdate(),
            upgrades_note=product.insert_3d_note,
            # Issue #19: what arrives for a PnP edition is files, not a
            # playable game — it still needs printing/prep.
            ready_status=(
                Copy.ReadyStatus.NOT_READY if edition.is_pnp
                else Copy.ReadyStatus.READY
            ),
        )
    for requirement in product.sleeve_requirements.select_related("card_size"):
        SleeveRequirement.objects.get_or_create(
            edition=edition, card_size=requirement.card_size,
            defaults={"count": requirement.count},
        )
    product.edition = edition
    product.copy = copy
    product.save(update_fields=["edition", "copy"])
    # Issue #117: an arrived preorder becoming an active copy is the same
    # "own" transition as copy_add — push it to BGG.
    _enqueue_bgg_push(game, Game.BggCollectionStatus.OWN, request.user)
    # Edit the fresh copy, then return to the purchase to handle its remaining
    # items rather than landing on the game (#45) — the origin rides a query
    # param through the edit round-trip.
    url = reverse("copy_edit", args=[copy.pk])
    return redirect(f"{url}?from_purchase={product.wave.purchase_id}")


# ---------------------------------------------------------------------------
# Pledge planner (issue #186): pre-backing decision support scoped to one
# Purchase — compares candidate bundles by cost and want-priority coverage,
# using only what the campaign itself offers (single price per item, no
# multi-vendor comparison). Add/edit follow the purchase_add/purchase_edit
# ModelForm shape rather than this app's more common hand-parsed POST.
# ---------------------------------------------------------------------------

@login_required
def pledge_plan_add(request, purchase_pk):
    purchase = get_object_or_404(Purchase, pk=purchase_pk, owner=request.user)
    if hasattr(purchase, "pledge_plan"):
        return redirect("pledge_plan_detail", purchase_pk=purchase.pk)
    plan = PledgePlan(purchase=purchase)
    if request.method != "POST":
        return render(request, "pledge_plan_add.html", {
            "purchase": purchase,
            "plan_form": PledgePlanForm(instance=plan),
        })
    plan_form = PledgePlanForm(request.POST, instance=plan)
    if not plan_form.is_valid():
        return render(request, "pledge_plan_add.html", {
            "purchase": purchase, "plan_form": plan_form,
        })
    plan = plan_form.save()
    return redirect("pledge_plan_detail", purchase_pk=purchase.pk)


@login_required
def pledge_plan_edit(request, purchase_pk):
    """Edit an existing plan's currency/VAT rate/CZK rate — the settings
    entered once on pledge_plan_add, revisited as the campaign's numbers
    firm up."""
    plan = get_object_or_404(
        PledgePlan, purchase__pk=purchase_pk, purchase__owner=request.user,
    )
    if request.method != "POST":
        return render(request, "pledge_plan_add.html", {
            "purchase": plan.purchase, "plan": plan,
            "plan_form": PledgePlanForm(instance=plan),
        })
    plan_form = PledgePlanForm(request.POST, instance=plan)
    if not plan_form.is_valid():
        return render(request, "pledge_plan_add.html", {
            "purchase": plan.purchase, "plan": plan, "plan_form": plan_form,
        })
    plan_form.save()
    return redirect("pledge_plan_detail", purchase_pk=plan.purchase_id)


def _pledge_plan_table_context(plan, view_mode="auto"):
    """view_mode="all" forces every bundle; otherwise, once any bundle is
    starred, the table narrows to just the shortlisted ones (issue #186
    follow-up: "I usually narrow down to a couple and want to hide the
    rest")."""
    all_bundles = list(plan.bundles.all())
    shortlisted = [b for b in all_bundles if b.is_shortlisted]
    showing_shortlisted_only = bool(shortlisted) and view_mode != "all"
    return {
        "plan": plan,
        "purchase": plan.purchase,
        "items": plan.items.all(),
        "bundles": shortlisted if showing_shortlisted_only else all_bundles,
        "all_bundles_count": len(all_bundles),
        "shortlisted_count": len(shortlisted),
        "showing_shortlisted_only": showing_shortlisted_only,
        "view_mode": view_mode,
        "priority_choices": Game.WishlistPriority.choices,
    }


@login_required
def pledge_plan_detail(request, purchase_pk):
    plan = get_object_or_404(
        PledgePlan.objects.select_related("purchase")
        .prefetch_related("items", "bundles__items"),
        purchase__pk=purchase_pk, purchase__owner=request.user,
    )
    view_mode = request.GET.get("view", "auto")
    return render(request, "pledge_plan_detail.html",
                  _pledge_plan_table_context(plan, view_mode))


@login_required
def pledge_plan_item_add(request, purchase_pk):
    plan = get_object_or_404(
        PledgePlan, purchase__pk=purchase_pk, purchase__owner=request.user,
    )
    item = PledgePlanItem(plan=plan)
    if request.method != "POST":
        return render(request, "pledge_plan_item_edit.html", {
            "plan": plan, "purchase": plan.purchase,
            "item_form": PledgePlanItemForm(instance=item, plan=plan),
        })
    item_form = PledgePlanItemForm(request.POST, instance=item, plan=plan)
    if not item_form.is_valid():
        return render(request, "pledge_plan_item_edit.html", {
            "plan": plan, "purchase": plan.purchase, "item_form": item_form,
        })
    item_form.save()
    return redirect("pledge_plan_detail", purchase_pk=plan.purchase_id)


@login_required
def pledge_plan_item_edit(request, pk):
    item = get_object_or_404(
        PledgePlanItem.objects.select_related("plan__purchase"),
        pk=pk, plan__purchase__owner=request.user,
    )
    plan = item.plan
    if request.method != "POST":
        return render(request, "pledge_plan_item_edit.html", {
            "plan": plan, "purchase": plan.purchase, "item": item,
            "item_form": PledgePlanItemForm(instance=item, plan=plan),
        })
    item_form = PledgePlanItemForm(request.POST, instance=item, plan=plan)
    if not item_form.is_valid():
        return render(request, "pledge_plan_item_edit.html", {
            "plan": plan, "purchase": plan.purchase, "item": item,
            "item_form": item_form,
        })
    item_form.save()
    return redirect("pledge_plan_detail", purchase_pk=plan.purchase_id)


@login_required
@require_POST
def pledge_plan_item_delete(request, pk):
    item = get_object_or_404(
        PledgePlanItem.objects.select_related("plan"),
        pk=pk, plan__purchase__owner=request.user,
    )
    purchase_pk = item.plan.purchase_id
    item.delete()
    return redirect("pledge_plan_detail", purchase_pk=purchase_pk)


@login_required
def pledge_plan_bundle_add(request, purchase_pk):
    plan = get_object_or_404(
        PledgePlan, purchase__pk=purchase_pk, purchase__owner=request.user,
    )
    bundle = PledgePlanBundle(plan=plan)
    if request.method != "POST":
        return render(request, "pledge_plan_bundle_edit.html", {
            "plan": plan, "purchase": plan.purchase,
            "bundle_form": PledgePlanBundleForm(instance=bundle, plan=plan),
        })
    bundle_form = PledgePlanBundleForm(request.POST, instance=bundle, plan=plan)
    if not bundle_form.is_valid():
        return render(request, "pledge_plan_bundle_edit.html", {
            "plan": plan, "purchase": plan.purchase, "bundle_form": bundle_form,
        })
    bundle_form.save()
    return redirect("pledge_plan_detail", purchase_pk=plan.purchase_id)


@login_required
def pledge_plan_bundle_edit(request, pk):
    bundle = get_object_or_404(
        PledgePlanBundle.objects.select_related("plan__purchase"),
        pk=pk, plan__purchase__owner=request.user,
    )
    plan = bundle.plan
    if request.method != "POST":
        return render(request, "pledge_plan_bundle_edit.html", {
            "plan": plan, "purchase": plan.purchase, "bundle": bundle,
            "bundle_form": PledgePlanBundleForm(instance=bundle, plan=plan),
        })
    bundle_form = PledgePlanBundleForm(request.POST, instance=bundle, plan=plan)
    if not bundle_form.is_valid():
        return render(request, "pledge_plan_bundle_edit.html", {
            "plan": plan, "purchase": plan.purchase, "bundle": bundle,
            "bundle_form": bundle_form,
        })
    bundle_form.save()
    return redirect("pledge_plan_detail", purchase_pk=plan.purchase_id)


@login_required
@require_POST
def pledge_plan_bundle_delete(request, pk):
    bundle = get_object_or_404(
        PledgePlanBundle.objects.select_related("plan"),
        pk=pk, plan__purchase__owner=request.user,
    )
    purchase_pk = bundle.plan.purchase_id
    bundle.delete()
    return redirect("pledge_plan_detail", purchase_pk=purchase_pk)


@login_required
@require_POST
def pledge_plan_bundle_item_toggle(request, pk, item_pk):
    """Add/remove one item from one bundle's M2M — a lightweight per-checkbox
    action that doesn't fit the ModelForm shape used elsewhere on this page."""
    bundle = get_object_or_404(
        PledgePlanBundle, pk=pk, plan__purchase__owner=request.user,
    )
    item = get_object_or_404(PledgePlanItem, pk=item_pk, plan_id=bundle.plan_id)
    if bundle.items.filter(pk=item.pk).exists():
        bundle.items.remove(item)
    else:
        bundle.items.add(item)
    plan = bundle.plan
    view_mode = request.GET.get("view", "auto")
    if request.headers.get("HX-Request"):
        return render(request, "partials/pledge_plan_table.html",
                      _pledge_plan_table_context(plan, view_mode))
    return redirect(f"{reverse('pledge_plan_detail', args=[plan.purchase_id])}?view={view_mode}")


@login_required
@require_POST
def pledge_plan_bundle_shortlist_toggle(request, pk):
    """Star/unstar a bundle as one of the candidates under active
    consideration — starring the first one narrows the table to just the
    shortlisted bundles (see _pledge_plan_table_context)."""
    bundle = get_object_or_404(
        PledgePlanBundle, pk=pk, plan__purchase__owner=request.user,
    )
    bundle.is_shortlisted = not bundle.is_shortlisted
    bundle.save(update_fields=["is_shortlisted"])
    plan = bundle.plan
    view_mode = request.GET.get("view", "auto")
    if request.headers.get("HX-Request"):
        return render(request, "partials/pledge_plan_table.html",
                      _pledge_plan_table_context(plan, view_mode))
    return redirect(f"{reverse('pledge_plan_detail', args=[plan.purchase_id])}?view={view_mode}")


# ---------------------------------------------------------------------------
# DESIGN §3 viewer projection, used by two gates:
#   tier 4  /share/<token>/  — anonymous, gated by the unguessable share_token;
#   tiers 2+3  /g/<slug>/    — logged-in viewers admitted by Group.is_viewable_by
#                              (ShareGrant targets / server-public).
# Restricted projection either way: collection only (games a group member has
# an ACTIVE copy of — preorders have no Copy yet and archived copies left),
# and a curated safe field set: cover, title, BGG link, players, playtime,
# weight/rating, mechanics, owning group name. Personal signals (excitement,
# keep-status, location, notes, owners, purchases, sleeves) never render here
# — per §3 those are visible to the *group*, and a viewer is not in it.
# ---------------------------------------------------------------------------

def _shared_games(group, *, location=None):
    """Games in the group's shared collection: at least one member owns an
    active Copy. This is the whole viewer surface — pks outside it 404.
    location (issue #123) further pins the result to copies at that one
    Location — filtered in the same clause as group/active so it's the same
    Copy row that must satisfy all three conditions."""
    filters = {
        "editions__copies__archive_status": Copy.ArchiveStatus.ACTIVE,
        "editions__copies__owner__membership__group": group,
    }
    if location is not None:
        filters["editions__copies__location"] = location
    return Game.objects.filter(**filters).distinct()


def _owner_membership(user, group):
    """The user's Membership if they own this group, else None. Owning is
    what unlocks the §3 sharing settings."""
    if not user.is_authenticated:
        return None
    membership = getattr(user, "membership", None)
    if (membership is not None and membership.group_id == group.pk
            and membership.role == Membership.Role.OWNER):
        return membership
    return None


def _render_shared_collection(request, group, url_key, collection_url_name,
                              detail_url_name, *, location=None):
    """Cover grid of a group's shared collection with the GameChooser panel
    (issue #120), reusing the owner grid's filter machinery minus the owner-only
    axes — location and availability are the owner's shelf/lending state, which
    §3 hides from viewers. url_key is whatever the URL scheme keys the group by
    (share token or slug); collection_url_name/detail_url_name let the shared
    chooser + tiles point back at the right routes. location (issue #123), when
    given, further pins the grid to one Location — the filter axis itself stays
    hidden (owner_axes=False below), so a location-scoped visitor never sees a
    dropdown listing the group's other locations."""
    filters = _build_filters(request, owner_axes=False)
    games = _shared_games(group, location=location)
    if not filters["expansions"]:
        games = games.filter(type=Game.Type.BASE)
    games = games.select_related("series__primary_game").prefetch_related(
        "expansions__editions__copies", "expansions__game_types",
        "game_types", "game_tags",
        "editions__copies__location", "editions__copies__loans",
        "series__members", "families", "alternate_names",
    )
    matching = [game for game in games if _game_matches(game, filters)]
    # Incoming preorders are an owner-only signal (§6), so no series here carries
    # an incoming flag: viewers only ever see games with an ACTIVE copy.
    tiles = _collapse_into_tiles(
        matching, set(), collapse=filters["collapse"], url_key=url_key,
        detail_url_name=detail_url_name, series_url_name=None,
    )
    sort_columns = []
    if filters["view"] == "list":
        for tile in tiles:
            _annotate_list_row(tile)
        tiles = _sort_tiles_for_list(tiles, filters["sort"])
        sort_columns = _sort_column_context(filters["sort"], owner_view=False)
    chooser_url = reverse(collection_url_name, args=[url_key])
    # The dropdowns list only theme/family labels actually used by the shared
    # set, never the owner's full private vocabulary — no owner data leaks
    # through the filter options (issue #120). Game types are a fixed enum.
    shared_pks = list(
        _shared_games(group, location=location).values_list("pk", flat=True))
    context = {
        "group": group,
        "location": location,
        "url_key": url_key,
        "detail_url_name": detail_url_name,
        "games": matching,
        "tiles": tiles,
        "game_count": len(matching),
        "total_count": games.count(),
        "filters": filters,
        "owner_view": False,
        "chooser_url": chooser_url,
        "reset_url": chooser_url,
        "game_type_choices": GameType.Type.choices,
        "theme_tags": Tag.objects.filter(
            kind=Tag.Kind.THEME, games__pk__in=shared_pks).distinct(),
        "mechanic_tags": Tag.objects.filter(
            kind=Tag.Kind.MECHANIC, games__pk__in=shared_pks).distinct(),
        "family_choices": Family.objects.filter(
            members__pk__in=shared_pks).distinct(),
        # The owner browsing their own viewer page gets a shortcut into the
        # sharing settings; everyone else (viewers, anonymous) does not.
        "can_manage": _owner_membership(request.user, group) is not None,
        "sort_columns": sort_columns,
    }
    if request.headers.get("HX-Request"):
        return render(request, "partials/game_results.html", context)
    return render(request, "share_collection.html", context)


# Issue #121: crowdfunding/marketplace links are public game info like BGG;
# Drive/Dropbox stay hidden (personal storage, not public game info).
_SHARED_EXTERNAL_LINK_TYPES = {
    ExternalLink.LinkType.KICKSTARTER,
    ExternalLink.LinkType.GAMEFOUND,
    ExternalLink.LinkType.ZATROLENE,
}


def _render_shared_detail(request, group, pk, url_key,
                          collection_url_name, detail_url_name, *,
                          location=None):
    """Game detail, curated fields only. Shows the synced BGG stats, not
    per-user effective ranges — effective stats are per-user (§4) and a viewer
    has no copies here. Expansion cross-links stay inside the shared set.
    location (issue #123), when given, narrows both the lookup and the
    cross-link set to that one Location, so a location-scoped visitor can
    never reach a game/expansion that lives on a different shelf."""
    game = get_object_or_404(
        _shared_games(group, location=location).prefetch_related(
            "game_tags__tag", "bgg_links",
            # Issue #98: the shared expansions list renders short_name.
            "expansions__expands", "documents", "external_links",
        ),
        pk=pk,
    )
    shared_pks = set(
        _shared_games(group, location=location).values_list("pk", flat=True))
    mechanics = sorted(
        (gt.tag for gt in game.game_tags.all() if gt.tag.kind == Tag.Kind.MECHANIC),
        key=lambda tag: tag.name,
    )
    # Issue #121: every expansion, owned marked against this group/location's
    # own shared set (not a global "does anyone own it" check) — no owner
    # usernames, unlike the owner-side page, since usernames are never
    # exposed here.
    expansion_rows = sorted(
        ({"game": e, "owned": e.pk in shared_pks} for e in game.expansions.all()),
        key=lambda row: (not row["owned"], row["game"].name.lower()),
    )
    return render(request, "share_game_detail.html", {
        "group": group,
        "location": location,
        "url_key": url_key,
        "collection_url_name": collection_url_name,
        "detail_url_name": detail_url_name,
        "game": game,
        "mechanics": mechanics,
        "expansion_rows": expansion_rows,
        "expands": [b for b in game.expands.all() if b.pk in shared_pks],
        "external_links": [
            link for link in game.external_links.all()
            if link.link_type in _SHARED_EXTERNAL_LINK_TYPES
        ],
        "documents": game.documents.all(),
    })


def share_collection(request, token):
    """Anonymous cover grid (DESIGN §3 tier 4)."""
    group = get_object_or_404(Group, share_token=token)
    return _render_shared_collection(
        request, group, token, "share_collection", "share_game_detail",
    )


def share_game_detail(request, token, pk):
    """Anonymous game detail (DESIGN §3 tier 4)."""
    group = get_object_or_404(Group, share_token=token)
    return _render_shared_detail(
        request, group, pk, token, "share_collection", "share_game_detail",
    )


def share_location_collection(request, token):
    """Cover grid pinned to one Location, no login required (issue #123).
    Works the same for anonymous and logged-in visitors — like the tier-4
    group link, the token itself is the only gate."""
    location = get_object_or_404(Location.objects.select_related("group"),
                                  share_token=token)
    return _render_shared_collection(
        request, location.group, token, "share_location_collection",
        "share_location_game_detail", location=location,
    )


def share_location_game_detail(request, token, pk):
    """Game detail pinned to one Location, no login required (issue #123)."""
    location = get_object_or_404(Location.objects.select_related("group"),
                                  share_token=token)
    return _render_shared_detail(
        request, location.group, pk, token, "share_location_collection",
        "share_location_game_detail", location=location,
    )


def _viewable_group_or_404(request, slug):
    """Resolve /g/<slug>/ and apply the §3 tier gate. No access renders as
    404, not 403 — a private group's existence shouldn't be probeable, same
    stance as the token views."""
    group = get_object_or_404(Group, slug=slug)
    if not group.is_viewable_by(request.user):
        raise Http404("No shared collection here.")
    return group


@login_required
def group_collection(request, slug):
    """Logged-in viewer cover grid (DESIGN §3 tiers 2+3, plus members
    browsing their own group by slug)."""
    group = _viewable_group_or_404(request, slug)
    return _render_shared_collection(
        request, group, slug, "group_collection", "group_game_detail",
    )


@login_required
def group_game_detail(request, slug, pk):
    """Logged-in viewer game detail (DESIGN §3 tiers 2+3)."""
    group = _viewable_group_or_404(request, slug)
    return _render_shared_detail(
        request, group, pk, slug, "group_collection", "group_game_detail",
    )


# ---------------------------------------------------------------------------
# DESIGN §3 sharing settings: the group owner manages the visibility tier,
# ShareGrants (tier 2) and the anonymous share link (tier 4) without the
# admin. Owner-only ("manage shares & visibility" is the owner role's job);
# everyone else gets the same 404 stance as the viewer gate.
# ---------------------------------------------------------------------------

def _owned_group_or_404(request, slug):
    group = get_object_or_404(Group, slug=slug)
    if _owner_membership(request.user, group) is None:
        raise Http404("No sharing settings here.")
    return group


def _sharing_context(request, group, **extra):
    share_url = ""
    if group.share_token:
        share_url = request.build_absolute_uri(
            reverse("share_collection", args=[group.share_token]),
        )
    location_shares = [
        {
            "location": location,
            "share_url": request.build_absolute_uri(
                reverse("share_location_collection", args=[location.share_token]),
            ) if location.share_token else "",
        }
        for location in group.locations.order_by("name")
    ]
    return {
        "group": group,
        "grants": group.share_grants.select_related(
            "grantee_user", "grantee_group",
        ).order_by("created_at"),
        "invites": group.invites.select_related("invited_user").order_by("created_at"),
        "visibility_choices": Group.Visibility.choices,
        "share_url": share_url,
        "location_shares": location_shares,
        "viewer_url": request.build_absolute_uri(
            reverse("group_collection", args=[group.slug]),
        ),
        **extra,
    }


def _render_sharing(request, group, **extra):
    """htmx target: every settings POST swaps the whole settings block back
    in, so the controls always show the saved state."""
    return render(request, "partials/sharing_settings.html",
                  _sharing_context(request, group, **extra))


@login_required
def group_settings(request, slug):
    """The §3 sharing settings page for one group."""
    group = _owned_group_or_404(request, slug)
    return render(request, "group_settings.html",
                  _sharing_context(request, group))


@login_required
@require_POST
def group_settings_visibility(request, slug):
    """Switch the group's visibility tier. Grants and the share token are
    left alone — the tiers are modal (is_viewable_by), so grants on a
    private group are inert, not deleted."""
    group = _owned_group_or_404(request, slug)
    value = request.POST.get("visibility", "")
    if value not in Group.Visibility.values:
        return HttpResponseBadRequest("Unknown visibility tier.")
    group.visibility = value
    group.save(update_fields=["visibility"])
    return _render_sharing(request, group)


@login_required
@require_POST
def group_settings_share_link(request, slug):
    """Mint or revoke the tier-4 anonymous share token — the same two
    operations the admin actions offer."""
    group = _owned_group_or_404(request, slug)
    action = request.POST.get("action")
    if action == "enable":
        group.enable_share_link()
    elif action == "revoke":
        group.share_token = None
        group.save(update_fields=["share_token"])
    else:
        return HttpResponseBadRequest("Unknown share-link action.")
    return _render_sharing(request, group)


@login_required
@require_POST
def group_settings_location_share_link(request, slug, location_pk):
    """Mint or revoke a per-Location share token (issue #123) — same
    enable/revoke shape as the group-level tier-4 link."""
    group = _owned_group_or_404(request, slug)
    location = get_object_or_404(Location, pk=location_pk, group=group)
    action = request.POST.get("action")
    if action == "enable":
        location.enable_share_link()
    elif action == "revoke":
        location.share_token = None
        location.save(update_fields=["share_token"])
    else:
        return HttpResponseBadRequest("Unknown share-link action.")
    return _render_sharing(request, group)


@login_required
@require_POST
def group_settings_grant_add(request, slug):
    """Add a tier-2 ShareGrant to a user (by username) or a group (by
    slug). Validation problems render as a message in the swapped-in
    settings block, not an error status — the page stays usable."""
    group = _owned_group_or_404(request, slug)
    kind = request.POST.get("grantee_type")
    name = (request.POST.get("grantee") or "").strip()
    if kind not in ("user", "group"):
        return HttpResponseBadRequest("Unknown grantee type.")

    error, grantee = None, None
    if not name:
        error = "Enter a username or group slug."
    elif kind == "user":
        user = get_user_model().objects.filter(username=name).first()
        if user is None:
            error = f'No user named "{name}".'
        elif getattr(user, "membership", None) and user.membership.group_id == group.pk:
            error = f'"{name}" is a member and already sees the collection.'
        else:
            grantee = {"grantee_user": user}
    else:
        target = Group.objects.filter(slug=name).first()
        if target is None:
            error = f'No group with slug "{name}".'
        elif target.pk == group.pk:
            error = "That is this group."
        else:
            grantee = {"grantee_group": target}

    if grantee is not None:
        if group.share_grants.filter(**grantee).exists():
            error = f'"{name}" already has a grant.'
        else:
            ShareGrant.objects.create(group=group, **grantee)
    return _render_sharing(request, group, grant_error=error)


@login_required
@require_POST
def group_settings_grant_delete(request, slug, pk):
    """Revoke a grant — deleting the row, same as the admin."""
    group = _owned_group_or_404(request, slug)
    grant = get_object_or_404(ShareGrant, pk=pk, group=group)
    grant.delete()
    return _render_sharing(request, group)


@login_required
@require_POST
def group_settings_invite_add(request, slug):
    """Invite an existing user (by username) to join this group as a
    Member (DESIGN §3). Mirrors group_settings_grant_add's validation
    style — problems render as a message in the swapped-in settings
    block, not an error status."""
    group = _owned_group_or_404(request, slug)
    name = (request.POST.get("invitee") or "").strip()

    error, user = None, None
    if not name:
        error = "Enter a username."
    else:
        user = get_user_model().objects.filter(username=name).first()
        if user is None:
            error = f'No user named "{name}".'
        elif getattr(user, "membership", None) and user.membership.group_id == group.pk:
            error = f'"{name}" is already a member of this group.'
        elif group.invites.filter(invited_user=user).exists():
            error = f'"{name}" already has a pending invite.'

    if error is None:
        Invite.objects.create(group=group, invited_user=user)
    return _render_sharing(request, group, invite_error=error)


@login_required
@require_POST
def group_settings_invite_delete(request, slug, pk):
    """Revoke a pending invite — deleting the row."""
    group = _owned_group_or_404(request, slug)
    invite = get_object_or_404(Invite, pk=pk, group=group)
    invite.delete()
    return _render_sharing(request, group)


def _expansion_override_hint(expansion):
    """Compact summary of an expansion's §4 stat overrides for the owned-
    expansions list (issue #16), e.g. "5–6 players, +30 min". Empty when the
    expansion carries no overrides."""
    parts = []
    low, high = expansion.players_min_override, expansion.players_max_override
    if low or high:
        parts.append(f"{low or '?'}–{high or '?'} players")
    delta = expansion.playtime_delta_override
    if delta:
        parts.append(f"{delta:+d} min")
    return ", ".join(parts)


def _game_purchase_rows(user, games):
    """§6 backlink: the purchases containing any of these games (Product.game
    walked backwards). Owner-scoped like /purchases/ — purchases are personal,
    so each member sees only their own here. One row per purchase; when the
    game sits in several waves, the wave still on the way is the one whose
    ETA matters. Takes a list so the series detail page (issue #21) can feed
    a union over members; game_detail passes [game]. (Distinct from
    _purchase_rows above, which builds the /purchases/ pipeline table.)"""
    by_purchase = {}
    for product in (
        Product.objects.filter(game__in=games, wave__purchase__owner=user)
        .select_related("wave__purchase")
        .order_by("wave__number")
    ):
        wave = product.wave
        row = by_purchase.get(wave.purchase_id)
        if row is None:
            row = by_purchase[wave.purchase_id] = {
                "purchase": wave.purchase, "wave": wave, "in_collection": False,
            }
        elif (wave.status not in Wave.TERMINAL_STATUSES
                and row["wave"].status in Wave.TERMINAL_STATUSES):
            row["wave"] = wave
        if product.copy_id:
            row["in_collection"] = True
    for row in by_purchase.values():
        wave = row["wave"]
        row["eta"] = (
            (wave.expected_arrival or wave.original_eta)
            if wave.status not in Wave.TERMINAL_STATUSES else None
        )
    return sorted(by_purchase.values(), key=lambda row: (
        PURCHASE_STAGE_ORDER.get(row["purchase"].status, len(PURCHASE_STAGE_ORDER)),
        row["purchase"].name.lower(),
    ))


@login_required
def game_detail(request, pk):
    """Game detail (DESIGN §13 image-forward): hero cover, §10 taxonomy
    chips, effective stats, group copies and outbound links. Weight/mechanic
    chips wait for the BGG registered-app token (§15) like the chooser axes.
    """
    game = get_object_or_404(
        Game.objects.prefetch_related(
            "expansions__editions__copies__owner", "expands",
            # Issue #98: each expansion row/badge renders short_name, which
            # strips its base name — prefetch the reverse link to avoid N+1.
            "expansions__expands", "expansions__game_types",
            "game_types", "game_tags__tag", "families", "designers",
            "digital_implementations", "bgg_links", "external_links",
            "editions__copies__location", "editions__copies__owner",
            "editions__copies__loans", "expansions__editions__copies__loans",
            "documents", "expansions__documents", "alternate_names",
        ),
        pk=pk,
    )
    low, high = game.effective_player_range()
    # Issue #134: §10 taxonomy chips, base marks unioned with owned expansions'.
    effective_game_types = game.effective_game_types()
    copies = [
        copy
        for edition in game.editions.all()
        for copy in edition.copies.all()
    ]

    # Issue #17: a read-only cut of the §5 sleeve worklist for the viewer's own
    # active copies of this game — the collapsed Sleeves card. Editing lives on
    # the copy edit page; copies with no card sizes or statuses contribute
    # nothing (the card is hidden entirely).
    sleeve_copies = []
    for edition in game.editions.all():
        for copy in edition.copies.all():
            if (copy.owner_id == request.user.pk
                    and copy.archive_status == Copy.ArchiveStatus.ACTIVE):
                rows = _copy_sleeve_rows(copy, edition)
                if rows:
                    sleeve_copies.append(
                        {"copy": copy, "edition": edition, "rows": rows})

    # Issue #16 + #47: this game's expansions. An expansion counts as owned
    # once it has at least one active Copy (the same "owned" surface the shared
    # grid uses); owned rows carry their owners and the §4 override hint. Known-
    # but-unowned expansions (linked via expands, no active copy) follow as a
    # muted what-could-be-added overview. Each row links to the expansion's own
    # game page. Owned first, then by name.
    expansion_rows = []
    # Issue #97: surface owned expansions' §7 documents read-only on the base
    # page (canonical doc still lives + managed on the expansion). Same "owned"
    # test as the rows above; expansions with no docs contribute nothing.
    expansion_documents = []
    for expansion in game.expansions.all():
        owners = sorted({
            copy.owner.username
            for edition in expansion.editions.all()
            for copy in edition.copies.all()
            if copy.archive_status == Copy.ArchiveStatus.ACTIVE and not copy.is_borrowed_in
        })
        owned = bool(owners)
        expansion_rows.append({
            "game": expansion,
            "owners": owners,
            "override": _expansion_override_hint(expansion),
            "owned": owned,
        })
        if owned:
            docs = list(expansion.documents.all())
            if docs:
                expansion_documents.append({"expansion": expansion, "documents": docs})
    expansion_rows.sort(key=lambda row: (not row["owned"], row["game"].name.lower()))
    expansion_documents.sort(key=lambda group: group["expansion"].name.lower())
    owned_expansion_count = sum(1 for row in expansion_rows if row["owned"])

    themes = sorted(
        (gt for gt in game.game_tags.all() if gt.tag.kind == Tag.Kind.THEME),
        key=lambda gt: (not gt.is_favourite, gt.tag.name),
    )

    purchase_rows = _game_purchase_rows(request.user, [game])

    # §4 "I own this": the viewer can add a copy of any edition, including
    # ones they already own (issue #50 — duplicates warn, not block; a
    # rebuy of the same edition no longer needs a new Edition row). Games
    # with no editions yet get their default edition created on add.
    # Issue #43: a borrowed-in copy doesn't count as "owned" here — you can
    # still add an owned copy of an edition you're currently only borrowing,
    # without the duplicate-copy warning firing.
    editions = list(game.editions.all())
    owned_edition_pks = {
        copy.edition_id for copy in copies
        if copy.owner_id == request.user.pk and not copy.is_borrowed_in
    }

    return render(request, "game_detail.html", {
        "game": game,
        "players_low": low,
        "players_high": high,
        "effective_game_types": effective_game_types,
        "copies": copies,
        # Issue #17: the viewer's own copies with sleeve rows, read-only.
        "sleeve_copies": sleeve_copies,
        "themes": themes,
        "expansions": game.expansions.all(),
        "expands": game.expands.all(),
        "purchase_rows": purchase_rows,
        "expansion_rows": expansion_rows,
        "owned_expansion_count": owned_expansion_count,
        # Issue #53: the Editions card — every edition, addable or not.
        "editions": editions,
        # Issue #50: flags editions the Add-copy select should warn about.
        "owned_edition_pks": owned_edition_pks,
        # §7 documents: rulebooks/PnP/references, pinned ones first (Meta.ordering).
        "documents": game.documents.all(),
        # Issue #97: owned expansions' docs, grouped by source, read-only.
        "expansion_documents": expansion_documents,
        # Issue #65: the read-only plays log — most recent for this game, with
        # players prefetched. The full history lives on /plays/?game=<pk>.
        "recent_plays": list(
            game.plays.prefetch_related("players")[:PLAYS_ON_DETAIL]
        ),
        "plays_count": game.plays.count(),
        # Issue #117: a just-pushed status awaiting a confirming read.
        "push_pending": bgg_sync.push_is_pending(game, timezone.now()),
    })


# How many plays the game detail card shows before linking to the full feed.
PLAYS_ON_DETAIL = 10


@login_required
def plays(request):
    """Read-only plays feed (issue #65, DESIGN §8): recent plays across all
    games, or one game's full log with ?game=<pk>. GameKeeper never writes
    plays — these come from BGG (BG Stats' auto-post). Recent-first, capped."""
    queryset = (
        Play.objects.select_related("game")
        .prefetch_related("players")
    )
    game = None
    game_pk = request.GET.get("game")
    if game_pk:
        game = get_object_or_404(Game, pk=game_pk)
        queryset = queryset.filter(game=game)
    return render(request, "plays.html", {
        "plays": list(queryset[:PLAYS_FEED_LIMIT]),
        "game": game,
    })


# The global feed is a recent-plays surface, not a paginated archive — cap it.
PLAYS_FEED_LIMIT = 100


@login_required
@require_POST
def game_bgg_sync(request, pk):
    """Per-game on-demand BGG refresh (issue #44): re-pull this one game's
    BGG-sourced fields without the bulk sync. Respects the §8 rules (only
    BGG-synced fields + last_synced_at written), and returns an HTMX partial
    with a success/failure indication — bgg_sync.sync_game never raises."""
    game = get_object_or_404(Game, pk=pk)
    result = bgg_sync.sync_game(game, user=request.user)
    return render(request, "partials/_bgg_sync_status.html",
                  {"game": game, "sync": result})


def _enqueue_bgg_push(game, new_status, user, *, priority=None):
    """Fire-and-forget dispatch of the issue #117 write-back, shared by every
    hook that derives a BGG status change from an existing app action
    (copy_add, product_convert, curation_archive, wishlist_add/remove,
    _sync_preorder_status) — there is deliberately no manual "pick a status"
    control (DESIGN §8): the push always mirrors state the app already
    tracks natively. No-ops if the game has nothing to push to. Mirrors
    run_tool_command's dispatch (line ~2444): `.delay()` can raise with no
    broker/worker running (the dev reality — see tests.py's ToolRun
    coverage), so a failed enqueue must never break the calling action — it
    just surfaces on the dashboard like any other sync diff."""
    if game.primary_bgg_link is None:
        return
    try:
        push_bgg_status_task.delay(game.pk, new_status, user.pk, priority=priority)
    except Exception:
        bgg_sync.record_push_failure(
            game, user, "Could not queue the BGG push (worker unavailable).")


def _sync_preorder_status(game, user):
    """Issue #166: recompute whether `game` is currently "incoming" for
    `user` — the same COMMITTED-purchase + non-terminal-wave rule
    _incoming_rows uses for the dashboard card — and push/clear the BGG
    `preordered` flag to match. Recomputed from scratch across every
    Purchase/Wave/Product each time (rather than tracked as a delta) so a
    second in-flight preorder of the same game is a harmless no-op push, and
    dropping one of two still leaves the other's PREORDERED status intact.
    Only clears back to "" when PREORDERED is still the tracked status —
    never stomp a status set by something else (own/prev_owned/wishlist),
    same guard as wishlist_remove."""
    is_incoming = Product.objects.filter(
        game=game, copy__isnull=True,
        wave__purchase__owner=user,
        wave__purchase__status=Purchase.Status.COMMITTED,
        wave__status__in=INCOMING_STATUSES,
    ).exists()
    if is_incoming:
        if game.bgg_collection_status != Game.BggCollectionStatus.PREORDERED:
            _enqueue_bgg_push(game, Game.BggCollectionStatus.PREORDERED, user)
    elif game.bgg_collection_status == Game.BggCollectionStatus.PREORDERED:
        _enqueue_bgg_push(game, "", user)


def _enqueue_bgg_fortrade_push(game, fortrade, user):
    """Fire-and-forget dispatch of the issue #82 "for trade" write-back —
    the fortrade counterpart of _enqueue_bgg_push, dispatching the separate
    merge-based push_bgg_fortrade_task instead."""
    if game.primary_bgg_link is None:
        return
    try:
        push_bgg_fortrade_task.delay(game.pk, fortrade, user.pk)
    except Exception:
        bgg_sync.record_push_failure(
            game, user, "Could not queue the BGG push (worker unavailable).")


def _sync_leaving_status(game, user):
    """Issue #82: recompute whether `user` currently has any active copy of
    `game` marked keep_status=WILL_LEAVE, and push/clear the BGG `fortrade`
    flag to match. Recomputed from scratch (mirrors _sync_preorder_status)
    so it's a harmless no-op when nothing changed, and only clears back to
    False when fortrade is still the tracked pushed state — never stomps a
    push that raced in from elsewhere."""
    is_leaving = Copy.objects.filter(
        owner=user, edition__game=game, keep_status=Copy.KeepStatus.WILL_LEAVE,
        archive_status=Copy.ArchiveStatus.ACTIVE, is_borrowed_in=False,
    ).exists()
    if is_leaving:
        if not game.bgg_fortrade_pushed:
            _enqueue_bgg_fortrade_push(game, True, user)
    elif game.bgg_fortrade_pushed:
        _enqueue_bgg_fortrade_push(game, False, user)


# --- Tools page (issue #90) --------------------------------------------------
# Superuser-only in-app trigger for the bulk maintenance commands that used to
# need shell access: the full BGG sync and the cover-image download. Each runs
# off-request in a Celery task (tasks.run_tool_command); a ToolRun row tracks
# status/last-run and guards against overlapping runs of the same kind.

def _tool_status_context():
    """Per-kind latest run + running flag for the Tools page and its poll
    partial."""
    kinds = [
        {
            "key": ToolRun.Kind.BGG_SYNC,
            "label": "BGG sync",
            "description": "Pull stats/images from BGG and reconcile collection "
                           "membership for your account.",
            "running": ToolRun.is_running(ToolRun.Kind.BGG_SYNC),
            "latest": ToolRun.latest(ToolRun.Kind.BGG_SYNC),
        },
        {
            "key": ToolRun.Kind.DOWNLOAD_COVERS,
            "label": "Cover download",
            "description": "Download cover images for games missing one and bake "
                           "their square thumbnails.",
            "running": ToolRun.is_running(ToolRun.Kind.DOWNLOAD_COVERS),
            "latest": ToolRun.latest(ToolRun.Kind.DOWNLOAD_COVERS),
        },
        {
            "key": ToolRun.Kind.GENERATE_PREVIEWS,
            "label": "Cover previews",
            "description": "Rebuild the baked square cover previews for every "
                           "game, series and family from their current art and "
                           "focus/zoom/fit — run after changing the preview size "
                           "or fixing generation logic.",
            "running": ToolRun.is_running(ToolRun.Kind.GENERATE_PREVIEWS),
            "latest": ToolRun.latest(ToolRun.Kind.GENERATE_PREVIEWS),
        },
    ]
    return {"kinds": kinds, "any_running": any(k["running"] for k in kinds)}


@superuser_required
def tools(request):
    """The Tools console: buttons to trigger each maintenance run plus each
    kind's last-run status."""
    return render(request, "tools.html", _tool_status_context())


@superuser_required
@require_POST
def tools_run(request, kind):
    """Enqueue a maintenance run, unless one of the same kind is already in
    flight (the overlap guard). Returns the status partial either way."""
    if kind not in ToolRun.Kind.values:
        return HttpResponseBadRequest("Unknown tool.")
    if not ToolRun.is_running(kind):
        run = ToolRun.objects.create(kind=kind, triggered_by=request.user)
        try:
            run_tool_command.delay(run.pk)
        except Exception:
            # The broker/worker is unreachable (no Redis in dev, or a prod
            # hiccup). Don't leave a phantom `running` row wedging the overlap
            # guard forever — mark it failed so the partial swaps in with a
            # clear reason and the button re-enables.
            run.status = ToolRun.Status.FAILED
            run.finished_at = timezone.now()
            run.summary = (
                "Could not queue the job — the background worker or its Redis "
                "broker is unavailable. Start the Celery worker and try again."
            )
            run.save(update_fields=["status", "finished_at", "summary"])
    return render(request, "partials/_tools_status.html", _tool_status_context())


@superuser_required
def tools_status(request):
    """HTMX poll target: re-renders the status partial so a running job's
    result appears once its Celery task finishes."""
    return render(request, "partials/_tools_status.html", _tool_status_context())


@login_required
def game_add(request):
    """Create a game from just a BGG id or URL (issue #55) — the creation
    counterpart of the per-game re-sync. bgg_sync.create_game_from_bgg does
    the work (dedup, type probe, sync, name seeding) and never raises; this
    view only parses the input and routes the outcome: existing or created
    game -> its detail page, anything else -> the form with an inline error."""
    if request.method != "POST":
        return render(request, "game_add.html")
    raw = request.POST.get("bgg", "").strip()
    bgg_id = int(raw) if raw.isdigit() else extract_bgg_id(raw)
    if not bgg_id:
        return render(request, "game_add.html", {
            "error": "Enter a numeric BGG id or a boardgamegeek.com game URL.",
            "value": raw,
        })
    result = bgg_sync.create_game_from_bgg(bgg_id, user=request.user)
    if result.existing is not None:
        return redirect("game_detail", pk=result.existing.pk)
    if result.error:
        return render(request, "game_add.html", {"error": result.error, "value": raw})
    return redirect("game_detail", pk=result.game.pk)


def _bgg_import_form(request, *, error="", bgg_username=None, selected=None):
    """The status-picker form, for GET and for every re-render-with-error.
    bgg_username prefers the profile field, falling back to the synced
    account (settings.BGG_USERNAME) — for this app they are the same BGG
    login; the field mostly formalizes it (and survives the future rename)."""
    membership = getattr(request.user, "membership", None)
    if bgg_username is None:
        bgg_username = (membership.bgg_username if membership else "") or getattr(
            settings, "BGG_USERNAME", "",
        )
    return render(request, "bgg_import.html", {
        "error": error,
        "bgg_username": bgg_username,
        "status_choices": bgg_sync.IMPORT_STATUS_CHOICES,
        "selected": (
            bgg_sync.DEFAULT_IMPORT_STATUSES if selected is None else set(selected)
        ),
    })


@login_required
def bgg_import(request):
    """Bulk-import games from the user's BGG collection (issue #81).

    Three renders of one URL: GET shows the status-picker form; POST
    step=preview fetches the selected statuses and shows what would be
    created (rows already linked by BGG id are listed separately, never
    re-imported); POST step=import runs the confirmed rows through the
    single-game create path and renders a per-item summary. The confirm
    submission is trusted (hidden action inputs, strictly validated) — a
    single-user app importing its own data; the worst a forged row can do
    is import the user's own game with a different copy action, and the
    BggLink guard still dedups. Both service calls never raise."""
    if request.method != "POST":
        return _bgg_import_form(request)

    step = request.POST.get("step")
    if step == "preview":
        selected = request.POST.getlist("status")
        known = {param for param, _ in bgg_sync.IMPORT_STATUS_CHOICES}
        if set(selected) - known:
            return HttpResponseBadRequest("Unknown BGG collection status.")
        bgg_username = request.POST.get("bgg_username", "").strip()
        if not bgg_username:
            return _bgg_import_form(
                request, error="Enter your BGG username.", selected=selected,
            )
        if not selected:
            return _bgg_import_form(
                request, error="Pick at least one collection status.",
                bgg_username=bgg_username,
            )
        membership = getattr(request.user, "membership", None)
        if membership is not None and membership.bgg_username != bgg_username:
            membership.bgg_username = bgg_username
            membership.save(update_fields=["bgg_username"])
        # Request in display order — deterministic, and the merged flags are
        # order-independent (every payload carries the full flag set).
        preview = bgg_sync.fetch_collection_candidates(
            bgg_username,
            [p for p, _ in bgg_sync.IMPORT_STATUS_CHOICES if p in selected],
            user=request.user,
        )
        if preview.error:
            return _bgg_import_form(
                request, error=preview.error,
                bgg_username=bgg_username, selected=selected,
            )
        return render(request, "bgg_import_preview.html", {
            "preview": preview, "bgg_username": bgg_username,
            "grouped_candidates": bgg_sync.group_candidates_by_action(preview.candidates),
        })

    if step == "import":
        items = []
        fortrade_ids = set()
        for raw_id in request.POST.getlist("include"):
            if not raw_id.isdigit():
                return HttpResponseBadRequest("Bad BGG id.")
            action = request.POST.get(f"action_{raw_id}", "")
            if action not in bgg_sync.IMPORT_ACTIONS:
                return HttpResponseBadRequest("Bad import action.")
            items.append((int(raw_id), action))
            # Issue #82: carried alongside action_<id> so a for-trade row's
            # created copy starts out marked keep_status=WILL_LEAVE.
            if request.POST.get(f"fortrade_{raw_id}") == "1":
                fortrade_ids.add(int(raw_id))
        if not items:
            return _bgg_import_form(
                request, error="Nothing was selected to import.",
            )
        report = bgg_sync.import_collection_items(
            request.user, items, fortrade_ids=fortrade_ids)
        if report.error:
            return _bgg_import_form(request, error=report.error)
        return render(request, "bgg_import_done.html", {"report": report})

    return HttpResponseBadRequest("Unknown import step.")


@login_required
@require_POST
def invite_accept(request, pk):
    """Accept a pending Invite: re-point the invitee's Membership into the
    inviting group as a Member, then delete their old "group of one" (its
    Locations cascade; any Copy.location pointing there is SET_NULL, so no
    Copy is lost — see models.py Location/Copy)."""
    invite = get_object_or_404(Invite, pk=pk, invited_user=request.user)
    membership = request.user.membership
    old_group = membership.group
    with transaction.atomic():
        membership.group = invite.group
        membership.role = Membership.Role.MEMBER
        membership.save(update_fields=["group", "role"])
        invite.delete()
        old_group.delete()
    return redirect("settings")


@login_required
@require_POST
def invite_decline(request, pk):
    """Decline a pending Invite — deleting the row, membership unchanged."""
    invite = get_object_or_404(Invite, pk=pk, invited_user=request.user)
    invite.delete()
    return redirect("settings")


@login_required
def settings_page(request):
    """The general Settings page (issue #137); its first section is the per-user
    BGG credentials UI (issue #118).

    Structured to grow: further settings become additional sections/POST
    branches here rather than one-off pages. (Named settings_page, not settings,
    so it doesn't shadow the module-level django.conf settings import.)

    The BGG password is write-only: the stored secret is never rendered back —
    the page shows only whether one is set, plus inputs to replace or clear it. A
    blank password POST (with the clear box unchecked) leaves the stored value
    untouched, so saving just the username doesn't wipe the password. Env creds
    stay a fallback for users who set none (see resolve_bgg_credentials).
    Hand-rolled POST parsing, matching bgg_import's house style (no forms.py).

    The BGG and notifications sections are separate <form>s (issue #162), so
    each field is only touched when its own form posted — otherwise saving
    the ntfy topic would blank out the BGG username, and vice versa."""
    membership = getattr(request.user, "membership", None)
    if membership is None:
        return HttpResponseBadRequest("No membership for this user.")

    if request.method == "POST":
        fields = []
        if "bgg_username" in request.POST:
            membership.bgg_username = request.POST.get("bgg_username", "").strip()
            fields.append("bgg_username")
            if request.POST.get("clear_password") == "on":
                membership.set_bgg_password("")
                fields.append("bgg_password_encrypted")
            elif request.POST.get("bgg_password", ""):
                membership.set_bgg_password(request.POST["bgg_password"])
                fields.append("bgg_password_encrypted")
        if "ntfy_topic" in request.POST:
            membership.ntfy_topic = request.POST.get("ntfy_topic", "").strip()
            fields.append("ntfy_topic")
        if fields:
            membership.save(update_fields=fields)
        return redirect("settings")  # PRG; the re-GET shows the new state

    ntfy_test = request.GET.get("ntfy_test")
    email_test = request.GET.get("email_test")
    return render(request, "settings.html", {
        "bgg_username": membership.bgg_username,
        "has_password": membership.has_bgg_password,
        "env_fallback": bool(getattr(settings, "BGG_USERNAME", "")),
        "ntfy_topic": membership.ntfy_topic,
        "ntfy_test_result": ntfy_test if ntfy_test in ("ok", "fail") else None,
        "user_email": request.user.email,
        "email_test_result": email_test if email_test in ("ok", "fail") else None,
        "pending_invites": request.user.invites_received.select_related("group"),
    })


@login_required
@require_POST
def settings_ntfy_test(request):
    """Send a one-off test push to the user's already-saved ntfy topic
    (issue #162). Ignores any topic in the POST body — the Save button in
    settings.html always runs first, so this only ever tests what's actually
    persisted on Membership.ntfy_topic, never an unsaved edit."""
    membership = getattr(request.user, "membership", None)
    if membership is None or not membership.ntfy_topic:
        return redirect("settings")
    ok = ntfy.send_ntfy(
        membership.ntfy_topic,
        "Test notification",
        "This is a test push from GameKeeper.",
    )
    if not ok:
        logger.warning("Test ntfy push to topic %r failed.", membership.ntfy_topic)
    return redirect(f"{reverse('settings')}?ntfy_test={'ok' if ok else 'fail'}")


@login_required
@require_POST
def settings_email_test(request):
    """Send a one-off test email to the current user's account address
    (issue #171), mirroring settings_ntfy_test. Unlike send_ntfy, Django's
    send_mail raises on failure, so the fail-soft catch happens here. Caught
    broadly (not just SMTPException/OSError) because this button's whole job
    is to never crash and always report pass/fail — a misconfigured
    EMAIL_BACKEND can raise other exception types too."""
    if not request.user.email:
        return redirect("settings")
    try:
        send_mail(
            "GameKeeper: test email",
            "This is a test email from GameKeeper.",
            None,
            [request.user.email],
        )
        ok = True
    except Exception:
        logger.exception("Test email to %s failed.", request.user.email)
        ok = False
    return redirect(f"{reverse('settings')}?email_test={'ok' if ok else 'fail'}")


# ---------------------------------------------------------------------------
# Game editing (DESIGN §13): /games/<pk>/edit/ hosts the cover tools
# (replace + crop focal point) and a plain save-and-return form for the
# user-owned Game fields. BGG-synced stats are deliberately absent (§8
# would overwrite them); type/expands/editions stay structural — admin (and,
# for expands, the §8 sync per issue #40), never this form.
# ---------------------------------------------------------------------------

COVER_FETCH_TIMEOUT = 30


def _game_edit_context(game, user, game_form, **extra):
    # Families are hand-rendered checkboxes (client-side filter, custom
    # ids) rather than {{ form.families }} — sticky across a failed POST
    # the same way as series/family's own member tables.
    has_families_field = "families" in game_form.fields
    if game_form.is_bound and has_families_field:
        family_pks = set(game_form.data.getlist("families"))
    elif has_families_field:
        family_pks = {str(pk) for pk in game.families.values_list("pk", flat=True)}
    else:
        family_pks = set()
    return {
        "game": game,
        "game_form": game_form,
        # Issue #78: Series/Family are the game's own side of a relation
        # curated elsewhere (series_edit/family_edit) — only existing rows
        # are offered here, never a way to create new ones.
        "family_choices": Family.objects.all(),
        "family_pks": family_pks,
        **extra,
    }


@login_required
def game_edit(request, pk):
    """The game edit page (issue #28: GameForm). GET renders the cover tools
    + details form; POST saves the details and returns to the detail page."""
    game = get_object_or_404(Game, pk=pk)
    if request.method != "POST":
        return render(request, "game_edit.html", _game_edit_context(
            game, request.user, GameForm(instance=game)))

    form = GameForm(request.POST, instance=game)
    if not form.is_valid():
        return render(request, "game_edit.html",
                      _game_edit_context(game, request.user, form))
    game = form.save()

    # Issue #51: alternate names, one per line in a free-text box (deduped
    # and stripped by GameForm.clean_alternate_names) — replace the child
    # rows wholesale. Nothing references them, so the PK churn is harmless.
    game.alternate_names.all().delete()
    for alt in form.cleaned_data["alternate_names"]:
        game.alternate_names.create(name=alt)

    return redirect("game_detail", pk=game.pk)


def _cover_editor_urls(obj):
    """Per-model endpoints (and options) for the shared cover editor
    partial — the one place that knows which entity it is editing.
    Series and Family covers are optional (Series falls back to the primary
    member's art, a Family to its alphabetically-first member's), so they
    get the remove button; a Game cover has no "unset" state."""
    if isinstance(obj, Series):
        return {
            "cover_edit_url": reverse("series_cover_edit", args=[obj.pk]),
            "cover_focus_url": reverse("series_cover_focus", args=[obj.pk]),
            "cover_can_clear": True,
        }
    if isinstance(obj, Family):
        return {
            "cover_edit_url": reverse("family_cover_edit", args=[obj.pk]),
            "cover_focus_url": reverse("family_cover_focus", args=[obj.pk]),
            "cover_can_clear": True,
        }
    return {
        "cover_edit_url": reverse("game_cover_edit", args=[obj.pk]),
        "cover_focus_url": reverse("game_cover_focus", args=[obj.pk]),
    }


def _render_cover_editor(request, obj, error=""):
    # The context key stays "game" — cover_editor.html and cover_art.html
    # duck-type on it (any CoverArtModel instance satisfies the contract).
    return render(request, "partials/cover_editor.html", {
        "game": obj, "cover_error": error, **_cover_editor_urls(obj),
    })


def _replace_cover(obj, data, stem):
    """Validate image bytes and swap them in as the local cover file of
    any CoverArtModel instance. The caller picks the filename stem (games
    use the bgg id, series "series-<pk>"). Returns an error message, ""
    on success."""
    error, image = _validate_cover_image(data)
    if error:
        return error
    extension = COVER_EXTENSIONS[image.format]

    old_name = obj.cover_image.name if obj.cover_image else ""
    # The focal point and zoom described the old art — reset for the new one.
    obj.cover_focus_x = 50
    obj.cover_focus_y = 50
    obj.cover_zoom = 100
    # Art dimensions feed the aspect-aware fit-mode scale (issue #1).
    obj.cover_width, obj.cover_height = image.size
    # Saving while the old file still exists makes the storage pick a fresh
    # suffixed name, so the URL changes and browsers can't keep showing the
    # stale cached cover. The old file is deleted after.
    obj.cover_image.save(f"{stem}{extension}", ContentFile(data))
    if old_name and old_name != obj.cover_image.name:
        obj.cover_image.storage.delete(old_name)
    # Bake the square grid-tile thumbnail from the new art (issue #104).
    obj.regenerate_cover_preview()
    return ""


def _handle_cover_edit(request, obj, stem):
    """Replace a cover (DESIGN §13) with an uploaded file or an image URL
    fetched server-side. Bad input is a typed-in-form mistake, so it
    renders inline in the card (the sharing-settings pattern) rather than
    a bare 400."""
    upload = request.FILES.get("file")
    url = (request.POST.get("url") or "").strip()

    if upload and url:
        return _render_cover_editor(
            request, obj, "Choose either a file or a URL, not both.")
    if upload:
        data = upload.read()
    elif url:
        if not url.startswith(("http://", "https://")):
            return _render_cover_editor(
                request, obj, "The URL must start with http:// or https://.")
        try:
            response = requests.get(url, timeout=COVER_FETCH_TIMEOUT)
            response.raise_for_status()
        except requests.RequestException as error:
            return _render_cover_editor(request, obj, f"Download failed: {error}")
        data = response.content
    else:
        return _render_cover_editor(
            request, obj, "Choose a file or paste an image URL.")

    error = _replace_cover(obj, data, stem)
    return _render_cover_editor(request, obj, error)


def _handle_cover_focus(request, obj):
    """Set the crop focal point and/or zoom (DESIGN §13): the grid tiles
    crop square with object-fit: cover; the focus percentages feed
    object-position and the zoom scales the img toward that point. Zoom
    below 100 flips the tile to "fit" (whole art over the letterbox colour
    — see CoverArtModel.cover_fit). Per-field updates like curation_edit —
    the picker click and marker drag post x+y, the sliders/number inputs
    post one field alone. x and y are handled independently so a single-axis
    number input can move just that axis (issues #12/#13). Non-numeric input
    is a 400 (curation-style); out-of-range typed values clamp to the range."""
    updated = []
    if "x" in request.POST:
        try:
            x = int(request.POST["x"])
        except ValueError:
            return HttpResponseBadRequest("Focal point must be whole percentages.")
        obj.cover_focus_x = max(0, min(100, x))
        updated.append("cover_focus_x")
    if "y" in request.POST:
        try:
            y = int(request.POST["y"])
        except ValueError:
            return HttpResponseBadRequest("Focal point must be whole percentages.")
        obj.cover_focus_y = max(0, min(100, y))
        updated.append("cover_focus_y")
    if "zoom" in request.POST:
        try:
            zoom = int(request.POST["zoom"])
        except ValueError:
            return HttpResponseBadRequest("Zoom must be a whole percentage.")
        obj.cover_zoom = max(50, min(300, zoom))
        updated.append("cover_zoom")
    if "fit_color" in request.POST:
        # Fit-mode letterbox colour: the swatch sends #rrggbb, the paste
        # field may send it bare (PowerToys copies without the #) — accept
        # both. Empty (the Reset button, or clearing the field) clears it.
        color = request.POST["fit_color"].strip()
        if re.fullmatch(r"[0-9a-fA-F]{6}", color):
            color = f"#{color}"
        if color and not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
            return HttpResponseBadRequest("Colour must be RRGGBB.")
        obj.cover_fit_color = color
        updated.append("cover_fit_color")
    if updated:
        obj.save(update_fields=updated + ["updated_at"])
        # The crop inputs changed — re-bake the grid thumbnail (issue #104).
        if obj.cover_image:
            obj.regenerate_cover_preview()
    return _render_cover_editor(request, obj)


@login_required
@require_POST
def game_cover_edit(request, pk):
    """download_covers never overwrites an existing local file, so
    replacements made here survive its re-runs."""
    game = get_object_or_404(Game, pk=pk)
    link = game.primary_bgg_link
    stem = str(link.bgg_id) if link else f"game-{game.pk}"
    return _handle_cover_edit(request, game, stem)


@login_required
@require_POST
def game_cover_focus(request, pk):
    return _handle_cover_focus(request, get_object_or_404(Game, pk=pk))


@login_required
@require_POST
def series_cover_edit(request, pk):
    """Series covers are optional: "clear" removes the custom art and the
    tile falls back to the primary member's cover (Series.cover_source)."""
    series = get_object_or_404(Series, pk=pk)
    if "clear" in request.POST:
        if series.cover_image:
            series.cover_image.delete(save=True)
            series.clear_cover_preview()  # drop the baked thumbnail too (#104)
        return _render_cover_editor(request, series)
    return _handle_cover_edit(request, series, f"series-{series.pk}")


@login_required
@require_POST
def series_cover_focus(request, pk):
    return _handle_cover_focus(request, get_object_or_404(Series, pk=pk))


@login_required
@require_POST
def family_cover_edit(request, pk):
    """Family covers are optional (issue #42): "clear" removes the art and
    the detail hero falls back to the first member's cover
    (Family.cover_source)."""
    family = get_object_or_404(Family, pk=pk)
    if "clear" in request.POST:
        if family.cover_image:
            family.cover_image.delete(save=True)
            family.clear_cover_preview()  # drop the baked thumbnail too (#104)
        return _render_cover_editor(request, family)
    return _handle_cover_edit(request, family, f"family-{family.pk}")


@login_required
@require_POST
def family_cover_focus(request, pk):
    return _handle_cover_focus(request, get_object_or_404(Family, pk=pk))


# ---------------------------------------------------------------------------
# Series (DESIGN §4, issue #21): the detail page (union over members) and the
# in-app editor. Display layer only — nothing per-member is merged.
# ---------------------------------------------------------------------------

@login_required
def series_list(request):
    """Issue #80: overview grid of all series, cover + name + member count,
    each tile linking to its detail page."""
    series_rows = Series.objects.select_related("primary_game").annotate(
        member_count=Count("members"),
    ).order_by("name")
    return render(request, "series_list.html", {"series_rows": series_rows})


@login_required
def series_detail(request, pk):
    """Series detail: the game page's structure fed a union over members —
    hero stats from the primary member, one member grid, Copies and Purchases
    pooled across members (each row still names its game), the members'
    expansions pooled into one list (#127), and plays shown two ways: the
    summed per-member bgg_numplays breakdown plus a capped feed of recent
    actual Play rows pooled across members (#133)."""
    series = get_object_or_404(
        Series.objects.select_related("primary_game").prefetch_related(
            "members__editions__copies__location",
            "members__editions__copies__owner",
            "members__editions__copies__loans",
            "members__expansions__editions__copies__owner",
            "members__expansions__expands",
        ),
        pk=pk,
    )
    members = list(series.members.all())  # Meta-ordered (sort_name)
    # Reuse the prefetched member instance for the primary so
    # effective_player_range() walks warm caches.
    primary = next((m for m in members if m.pk == series.primary_game_id),
                   series.primary_game)
    low, high = primary.effective_player_range()
    copies = [
        copy
        for member in members
        for edition in member.editions.all()
        for copy in edition.copies.all()
    ]

    # Issue #127: pool every member's expansions into one flat list, mirroring
    # the game page (owned = at least one active Copy; owned rows carry owners
    # and the §4 override hint, unowned follow muted). Each row names the base
    # member it belongs to. Dedupe by pk so an expansion shared by two members
    # (and the owned-count badge) is not double-counted; members are Meta-
    # ordered so the first-seen base is deterministic. Owned first, then name.
    expansion_rows = []
    seen_expansions = set()
    for member in members:
        for expansion in member.expansions.all():
            if expansion.pk in seen_expansions:
                continue
            seen_expansions.add(expansion.pk)
            owners = sorted({
                copy.owner.username
                for edition in expansion.editions.all()
                for copy in edition.copies.all()
                if copy.archive_status == Copy.ArchiveStatus.ACTIVE and not copy.is_borrowed_in
            })
            expansion_rows.append({
                "game": expansion,
                "base": member,
                "owners": owners,
                "override": _expansion_override_hint(expansion),
                "owned": bool(owners),
            })
    expansion_rows.sort(key=lambda row: (not row["owned"], row["game"].name.lower()))
    owned_expansion_count = sum(1 for row in expansion_rows if row["owned"])

    plays_rows = [(m, m.bgg_numplays) for m in members if m.bgg_numplays]

    # Issue #133: the read-only plays log, pooled across members — most recent
    # first (Play.Meta ordering), capped like the game page. select_related the
    # game so each row can name which member it belongs to (show_game).
    member_plays = Play.objects.filter(game__in=members).select_related("game")
    recent_plays = list(
        member_plays.prefetch_related("players")[:PLAYS_ON_DETAIL]
    )

    # Issue #58: the bulk location-move form only concerns the viewer's own
    # active copies of the members, and only lists their group's locations.
    # Issue #77: expansion members are excluded — they usually travel with
    # their base game, so a bulk move skips them (mirrors
    # series_set_location's own exclusion below).
    membership = getattr(request.user, "membership", None)
    my_copy_count = sum(
        1 for copy in copies
        if copy.owner_id == request.user.pk
        and copy.archive_status == Copy.ArchiveStatus.ACTIVE
        and copy.edition.game.type != Game.Type.EXPANSION
    )
    return render(request, "series_detail.html", {
        "series": series,
        "members": members,
        "primary": primary,
        "players_low": low,
        "players_high": high,
        "copies": copies,
        "locations": (
            membership.group.locations.order_by("name") if membership else []
        ),
        "my_copy_count": my_copy_count,
        "purchase_rows": _game_purchase_rows(request.user, members),
        "expansion_rows": expansion_rows,
        "owned_expansion_count": owned_expansion_count,
        "plays_rows": plays_rows,
        "plays_total": sum(count for _, count in plays_rows),
        "recent_plays": recent_plays,
        "plays_count": member_plays.count(),
    })


@require_POST
@login_required
def series_set_location(request, pk):
    """Issue #58: move every active copy the *current user* owns of this
    series' member games to one location, in a single action. Scoped hard to
    ``owner=request.user`` and active copies — another owner's copies of the
    same members are never touched. A per-copy ``.save()`` (only for copies
    that actually change) keeps the simple-history movement log intact. Bulk
    requires a real target location; an empty/foreign pk is a 400, never a
    silent mass-clear. Issue #77: expansion members are skipped — they
    usually travel with their base game, so a bulk move never relocates
    them."""
    series = get_object_or_404(Series, pk=pk)

    location_pk = request.POST.get("location", "")
    membership = getattr(request.user, "membership", None)
    location = (
        membership.group.locations.filter(pk=location_pk).first()
        if membership and location_pk.isdigit() else None
    )
    if location is None:
        return HttpResponseBadRequest("Unknown location.")

    copies = Copy.objects.filter(
        owner=request.user,
        archive_status=Copy.ArchiveStatus.ACTIVE,
        edition__game__series=series,
    ).exclude(edition__game__type=Game.Type.EXPANSION)
    moved = 0
    for copy in copies:
        if copy.location_id == location.pk:
            continue
        copy.location = location
        copy.save()
        moved += 1

    url = reverse("series_detail", kwargs={"pk": series.pk})
    return redirect(f"{url}?moved={moved}")


def _series_edit_context(series, series_form, **extra):
    posted = series_form.data.getlist("members") if series_form.is_bound else None
    primary_raw = (
        series_form.data.get("primary_game") if series_form.is_bound
        else series_form.initial.get("primary_game")
    )
    return {
        "series": series,
        "series_form": series_form,
        # The form's own narrowed queryset (unclaimed-or-ours base games) —
        # no need to recompute it here.
        "candidates": series_form.fields["members"].queryset,
        "member_pks": (
            set(posted) if posted is not None
            else {str(pk) for pk in series_form.initial.get("members", [])}
        ),
        "primary_pk": str(primary_raw) if primary_raw is not None else None,
        **extra,
    }


def _save_series(request, series):
    """Shared create/update POST handler (issue #28: SeriesForm). The
    members/primary_game checkboxes edit the reverse side of Game.series,
    reconciled here after save (see SeriesForm's docstring for why
    primary_game isn't a Meta field)."""
    form = SeriesForm(request.POST, request.FILES, instance=series or Series())
    if not form.is_valid():
        return render(request, "series_edit.html",
                       _series_edit_context(series, form))
    form.instance.primary_game = form.cleaned_data["primary_game"]
    series = form.save()

    # Reconcile membership: unchecked members are released (SET_NULL
    # semantics by hand), checked ones claimed.
    member_pks = {game.pk for game in form.cleaned_data["members"]}
    series.members.exclude(pk__in=member_pks).update(series=None)
    Game.objects.filter(pk__in=member_pks).update(series=series)

    # Only the add page still posts a cover here (issue #54) — once the
    # series exists, the htmx cover editor owns upload/URL/clear via
    # series_cover_edit. Pre-validated in the form, so no error can surface.
    if getattr(form, "cover_data", None):
        _replace_cover(series, form.cover_data, f"series-{series.pk}")

    return redirect("series_detail", pk=series.pk)


@login_required
def series_add(request):
    if request.method != "POST":
        return render(request, "series_edit.html",
                       _series_edit_context(None, SeriesForm()))
    return _save_series(request, None)


@login_required
def series_edit(request, pk):
    series = get_object_or_404(Series, pk=pk)
    if request.method != "POST":
        return render(request, "series_edit.html",
                       _series_edit_context(series, SeriesForm(instance=series)))
    return _save_series(request, series)


# ---------------------------------------------------------------------------
# Family (DESIGN §4, issue #42): the detail page (member grid, no pooling —
# members are distinct games, their Copies/Purchases live on their own
# pages) and the in-app editor mirroring the series one.
# ---------------------------------------------------------------------------

@login_required
def family_list(request):
    """Issue #80: overview grid of all families, cover + name + member
    count, each tile linking to its detail page."""
    family_rows = Family.objects.prefetch_related("members").annotate(
        member_count=Count("members"),
    ).order_by("name")
    return render(request, "family_list.html", {"family_rows": family_rows})


@login_required
def family_detail(request, pk):
    """Family detail: hero (own cover, else the alphabetically-first
    member's — Family.cover_source), curation note, BGG family link and the
    member grid. Deliberately no pooled stats or copies — unlike a Series,
    family members are genuinely different games."""
    family = get_object_or_404(
        Family.objects.prefetch_related("members"), pk=pk,
    )
    return render(request, "family_detail.html", {
        "family": family,
        "members": list(family.members.all()),  # Meta-ordered (sort_name)
    })


def _family_edit_context(family, family_form, **extra):
    """Candidates are ALL base games — membership is a loose M2M (a game may
    sit in several families), so joining one never steals from another and
    no unclaimed restriction applies. Expansions stay out: a family relates
    standalone games, expansions already hang off their base."""
    posted = family_form.data.getlist("members") if family_form.is_bound else None
    return {
        "family": family,
        "family_form": family_form,
        "candidates": Game.objects.filter(type=Game.Type.BASE),
        "member_pks": (
            set(posted) if posted is not None
            else {str(pk) for pk in family_form.initial.get("members", [])}
        ),
        **extra,
    }


def _save_family(request, family):
    """Shared create/update POST handler (issue #28: FamilyForm). Membership
    is the M2M's forward side — one set() reconciles both directions."""
    form = FamilyForm(request.POST, request.FILES, instance=family or Family())
    if not form.is_valid():
        return render(request, "family_edit.html",
                       _family_edit_context(family, form))
    family = form.save()
    family.members.set(form.cleaned_data["members"])

    # Only the add page still posts a cover here (the #54 pattern) — once
    # the family exists, the htmx cover editor owns upload/URL/clear via
    # family_cover_edit. Pre-validated in the form, so no error can surface.
    if getattr(form, "cover_data", None):
        _replace_cover(family, form.cover_data, f"family-{family.pk}")

    return redirect("family_detail", pk=family.pk)


@login_required
def family_add(request):
    if request.method != "POST":
        return render(request, "family_edit.html",
                       _family_edit_context(None, FamilyForm()))
    return _save_family(request, None)


@login_required
def family_edit(request, pk):
    family = get_object_or_404(Family, pk=pk)
    if request.method != "POST":
        return render(request, "family_edit.html",
                       _family_edit_context(family, FamilyForm(instance=family)))
    return _save_family(request, family)


# ---------------------------------------------------------------------------
# Copy add/edit (DESIGN §4): the first user-facing way to create and edit
# Copies — until now they came only from the importer, the BGG sync or the
# admin. Curation keeps the cull fields, sleeves keep sleeve statuses; this
# page owns the physical copy itself: edition, acquired date, location and
# the upgrade/customization columns. Archiving stays on /curation/;
# un-archive stays admin-only.
# ---------------------------------------------------------------------------

def _copy_edit_context(copy, copy_form, **extra):
    return {
        "copy": copy,
        "game": copy.edition.game,
        "copy_form": copy_form,
        # Issue #17: the editable per-copy sleeve table (same shared partial as
        # the §5 worklist and the game-detail read-only card).
        "sleeve_rows": _copy_sleeve_rows(copy),
        "sleeve_status_choices": CopySleeveStatus.Status.choices,
        **extra,
    }


@login_required
@require_POST
def copy_add(request, pk):
    """Create a copy of a game ("I own this") and jump to its edit page.
    The edition comes from the game-detail select, which now offers every
    edition including already-owned ones (issue #50) — the select's
    duplicate-copy modal is the intent voucher for those, ridden along as
    confirm_duplicate_copy; without it the POST 400s (mirrors
    _save_edition's confirm_default_switch)."""
    game = get_object_or_404(Game, pk=pk)
    edition_pk = request.POST.get("edition", "")
    if edition_pk:
        try:
            edition = game.editions.get(pk=int(edition_pk))
        except (ValueError, Edition.DoesNotExist):
            return HttpResponseBadRequest("Unknown edition.")
    elif game.editions.exists():
        return HttpResponseBadRequest("Pick an edition.")
    else:
        edition = Edition.objects.create(game=game, is_default=True)
    already_owned = Copy.objects.filter(
        owner=request.user, edition=edition, is_borrowed_in=False).exists()
    if already_owned and request.POST.get("confirm_duplicate_copy") != "1":
        return HttpResponseBadRequest(
            "You already have a copy of this edition — confirm to add another.")
    copy = Copy.objects.create(owner=request.user, edition=edition)
    # Issue #117: owning a copy now is the "own" signal — push it to BGG.
    _enqueue_bgg_push(game, Game.BggCollectionStatus.OWN, request.user)
    return redirect("copy_edit", pk=copy.pk)


def _resolve_counterparty(raw):
    """Issue #43: a loan's other party may or may not be a registered app
    user — try an exact User.username match first (mirrors
    group_settings_invite_add's resolution) and fall back to storing
    whatever was typed as a plain name. Returns (counterparty_user,
    counterparty_name), exactly one of which is set (or both empty)."""
    name = (raw or "").strip()
    if not name:
        return None, ""
    user = get_user_model().objects.filter(username=name).first()
    return (user, "") if user else (None, name)


@login_required
@require_POST
def copy_add_borrowed(request, pk):
    """Create a borrowed-in copy ("I'm borrowing this") and jump to its edit
    page — sibling of copy_add, but for the reverse direction (issue #43).
    Borrowed copies are never unique-per-edition (you can borrow a copy of a
    game you already own, or several from different lenders), so the
    duplicate-edition guard copy_add applies doesn't apply here. Never
    pushed to BGG (issue #117): a borrowed copy is present but not owned."""
    game = get_object_or_404(Game, pk=pk)
    edition_pk = request.POST.get("edition", "")
    if edition_pk:
        try:
            edition = game.editions.get(pk=int(edition_pk))
        except (ValueError, Edition.DoesNotExist):
            return HttpResponseBadRequest("Unknown edition.")
    elif game.editions.exists():
        return HttpResponseBadRequest("Pick an edition.")
    else:
        edition = Edition.objects.create(game=game, is_default=True)

    counterparty_user, counterparty_name = _resolve_counterparty(
        request.POST.get("counterparty", ""))
    if counterparty_user is None and not counterparty_name:
        return HttpResponseBadRequest("Say who you're borrowing this from.")

    copy = Copy.objects.create(
        owner=request.user, edition=edition, is_borrowed_in=True)
    Loan.objects.create(
        copy=copy, direction=Loan.Direction.BORROWED_IN,
        counterparty_user=counterparty_user, counterparty_name=counterparty_name,
        since=timezone.localdate(),
    )
    return redirect("copy_edit", pk=copy.pk)


@login_required
@require_POST
def copy_loan_out(request, pk):
    """Lend an owned, active copy out to someone (issue #43) — independent
    of Location, so lending no longer needs a dedicated per-borrower
    Location. 400s if the copy is already on loan (return it first) or is
    itself borrowed-in (you can't lend out what isn't yours)."""
    copy = get_object_or_404(
        Copy, pk=pk, owner=request.user,
        archive_status=Copy.ArchiveStatus.ACTIVE, is_borrowed_in=False,
    )
    if copy.active_loan is not None:
        return HttpResponseBadRequest("This copy is already on loan.")

    counterparty_user, counterparty_name = _resolve_counterparty(
        request.POST.get("counterparty", ""))
    if counterparty_user is None and not counterparty_name:
        return HttpResponseBadRequest("Say who you're lending this to.")

    Loan.objects.create(
        copy=copy, direction=Loan.Direction.LENT_OUT,
        counterparty_user=counterparty_user, counterparty_name=counterparty_name,
        since=parse_date(request.POST.get("since", "")) or timezone.localdate(),
        expected_return_date=parse_date(request.POST.get("expected_return_date", "")),
    )
    return redirect("copy_edit", pk=copy.pk)


@login_required
@require_POST
def copy_loan_return(request, pk):
    """Mark a copy's active loan returned (issue #43). A lent-out copy just
    comes back into normal rotation; a borrowed-in copy also archives on
    return — it was never really the owner's to keep — which retains play
    history for free (Play is keyed off Game, not Copy, so archiving a Copy
    never touches it)."""
    copy = get_object_or_404(
        Copy, pk=pk, owner=request.user, archive_status=Copy.ArchiveStatus.ACTIVE,
    )
    loan = copy.active_loan
    if loan is None:
        return HttpResponseBadRequest("This copy isn't on loan.")
    loan.returned_at = timezone.localdate()
    loan.save(update_fields=["returned_at"])

    if loan.direction == Loan.Direction.BORROWED_IN:
        copy.archive_status = Copy.ArchiveStatus.ARCHIVED
        copy.archive_reason = Copy.ArchiveReason.RETURNED
        copy.archive_date = timezone.localdate()
        copy.save(update_fields=[
            "archive_status", "archive_reason", "archive_date", "updated_at",
        ])
        return redirect("game_detail", pk=copy.edition.game_id)
    return redirect("copy_edit", pk=copy.pk)


@login_required
def copy_edit(request, pk):
    """The copy edit page (issue #136): one save for the details (edition,
    acquired date, location, upgrades) and the §11 curation signals
    (excitement, keep-status, immune, why-it-might-leave) together, so
    editing both and saving once can no longer silently drop one of them.
    Owner-scoped and active-only — archived copies are a browse-only
    shelf."""
    copy = get_object_or_404(
        Copy.objects.select_related("edition__game"),
        pk=pk, owner=request.user, archive_status=Copy.ArchiveStatus.ACTIVE,
    )
    game = copy.edition.game
    if request.method != "POST":
        # A copy reached by converting a purchase item returns to that purchase
        # on save (#45); anything else falls back to the game detail.
        raw = request.GET.get("from_purchase", "")
        return_purchase = raw if (
            raw.isdigit()
            and Purchase.objects.filter(pk=raw, owner=request.user).exists()
        ) else ""
        copy_form = CopyForm(instance=copy, owner=request.user, game=game)
        return render(request, "copy_edit.html",
                      _copy_edit_context(copy, copy_form,
                                         return_purchase=return_purchase))

    copy_form = CopyForm(
        request.POST, instance=copy, owner=request.user, game=game)
    if not copy_form.is_valid():
        return render(request, "copy_edit.html", _copy_edit_context(
            copy, copy_form,
            return_purchase=request.POST.get("return_purchase", "")))
    copy_form.save()
    _sync_leaving_status(game, request.user)
    return_purchase = request.POST.get("return_purchase", "")
    if (return_purchase.isdigit()
            and Purchase.objects.filter(
                pk=return_purchase, owner=request.user).exists()):
        return redirect("purchase_detail", pk=return_purchase)
    return redirect("game_detail", pk=game.pk)


# ---------------------------------------------------------------------------
# Edition add/edit (issue #53): the first user-facing way to create and edit
# Editions — until now they came only from the importer or the admin. One
# shared template and POST handler (series_edit style); both pages return to
# the game detail on save. Switching the default edition goes through a
# confirm step that also renames the demoted (usually blank-named) old
# default, keeping unique_default_edition_per_game satisfied atomically.
# ---------------------------------------------------------------------------

def _edition_edit_context(game, edition, **extra):
    # The current default other than the edition being edited feeds the
    # switch-confirm modal (and its rename field). When THIS edition is the
    # default, the constraint guarantees there is no other.
    other_default = game.editions.filter(is_default=True)
    if edition is not None:
        other_default = other_default.exclude(pk=edition.pk)
    context = {
        "game": game,
        "edition": edition,
        "other_default": other_default.first(),
        **extra,
    }
    # The sleeve-requirement editor (issue #129) keys off a saved edition pk,
    # so it only rides along when editing an existing edition, not on add.
    if edition is not None:
        context.update(_requirement_editor_context(edition))
    return context


def _save_edition(request, game, edition):
    """Shared create/update POST handler (issue #28: EditionForm). Blank name
    is legal — it reads as "default edition" everywhere."""
    is_new = edition is None
    form = EditionForm(request.POST, instance=edition or Edition(game=game), game=game)
    if not form.is_valid():
        return render(request, "edition_edit.html", _edition_edit_context(
            game, None if is_new else edition, edition_form=form))
    with transaction.atomic():
        if form.old_default is not None:
            form.old_default.is_default = False
            form.old_default.name = form.cleaned_data["old_default_name"].strip()
            form.old_default.save()
        edition = form.save()
    return redirect("game_detail", pk=game.pk)


@login_required
def edition_add(request, pk):
    game = get_object_or_404(Game, pk=pk)
    if request.method != "POST":
        return render(request, "edition_edit.html", _edition_edit_context(
            game, None, edition_form=EditionForm(instance=Edition(game=game), game=game)))
    return _save_edition(request, game, None)


@login_required
def edition_edit(request, pk):
    edition = get_object_or_404(Edition.objects.select_related("game"), pk=pk)
    if request.method != "POST":
        return render(request, "edition_edit.html", _edition_edit_context(
            edition.game, edition,
            edition_form=EditionForm(instance=edition, game=edition.game)))
    return _save_edition(request, edition.game, edition)


# --- §7 documents ----------------------------------------------------------


def _validate_document_upload(uploaded):
    """Enforce the §7 upload guards (settings). Returns an error string, or
    None when the file is acceptable. External-link-only docs skip this."""
    ext = uploaded.name.rsplit(".", 1)[-1].lower() if "." in uploaded.name else ""
    if ext not in settings.DOCUMENT_ALLOWED_EXTENSIONS:
        allowed = ", ".join(settings.DOCUMENT_ALLOWED_EXTENSIONS)
        return f"File type “.{ext}” is not allowed. Allowed types: {allowed}."
    if uploaded.size > settings.DOCUMENT_MAX_UPLOAD_SIZE:
        cap_mb = settings.DOCUMENT_MAX_UPLOAD_SIZE // (1024 * 1024)
        return f"File is too large — the limit is {cap_mb} MB."
    return None


@login_required
def document_add(request, pk):
    """Attach a §7 document to a game: an external URL and/or an uploaded file
    (both may coexist). Mirrors the edition add flow — hand-parsed POST, back
    to the game on success."""
    game = get_object_or_404(Game, pk=pk)
    if request.method != "POST":
        return render(request, "document_form.html", {
            "game": game,
            "doc_types": Document.Type.choices,
        })

    doc_type = request.POST.get("doc_type", Document.Type.OTHER)
    if doc_type not in Document.Type.values:
        return HttpResponseBadRequest("Unknown document type.")
    external_url = request.POST.get("external_url", "").strip()
    uploaded = request.FILES.get("file")
    if not external_url and not uploaded:
        return HttpResponseBadRequest(
            "A document needs an external URL, an uploaded file, or both.")
    if uploaded is not None:
        error = _validate_document_upload(uploaded)
        if error:
            return HttpResponseBadRequest(error)

    document = Document(
        content_object=game,
        doc_type=doc_type,
        label=request.POST.get("label", "").strip(),
        external_url=external_url,
        is_primary="is_primary" in request.POST,
    )
    if uploaded is not None:
        document.file = uploaded
    document.save()
    return redirect("game_detail", pk=game.pk)


def _document_redirect(document):
    """Send back to the host object's page. Only games have a document UI so
    far, so a non-game host falls back to the collection."""
    host = document.content_object
    if isinstance(host, Game):
        return redirect("game_detail", pk=host.pk)
    return redirect("collection")


@login_required
def document_edit(request, pk):
    """Edit an existing §7 document — type, label, external URL, uploaded file
    and the pin-as-primary flag (issue #97 moved priority + delete here off the
    game card). GET pre-fills the shared form; POST mirrors document_add, except
    an existing file already satisfies the URL/file invariant."""
    document = get_object_or_404(Document, pk=pk)
    if request.method != "POST":
        return render(request, "document_form.html", {
            "game": document.content_object,
            "document": document,
            "doc_types": Document.Type.choices,
        })

    doc_type = request.POST.get("doc_type", Document.Type.OTHER)
    if doc_type not in Document.Type.values:
        return HttpResponseBadRequest("Unknown document type.")
    external_url = request.POST.get("external_url", "").strip()
    uploaded = request.FILES.get("file")
    if not external_url and not uploaded and not document.file:
        return HttpResponseBadRequest(
            "A document needs an external URL, an uploaded file, or both.")
    if uploaded is not None:
        error = _validate_document_upload(uploaded)
        if error:
            return HttpResponseBadRequest(error)

    document.doc_type = doc_type
    document.label = request.POST.get("label", "").strip()
    document.external_url = external_url
    document.is_primary = "is_primary" in request.POST
    if uploaded is not None:
        if document.file:
            document.file.delete(save=False)
        document.file = uploaded
    document.save()
    return _document_redirect(document)


@login_required
@require_POST
def document_delete(request, pk):
    """Remove a document, deleting its uploaded file too (external-link-only
    docs have no file to clear)."""
    document = get_object_or_404(Document, pk=pk)
    redirect_response = _document_redirect(document)
    if document.file:
        document.file.delete(save=False)
    document.delete()
    return redirect_response
