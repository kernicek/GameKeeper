"""Celery tasks: the DESIGN §11 email reminders, plus the issue #117 BGG
write-back push.

Two reminder kinds, one daily digest email per owner (from Celery beat):
pledge managers closing soon, and campaigns ending soon (watched-but-
unbacked). No overdue-wave reminders in v1 (DESIGN §11). ReminderLog keeps
the task idempotent — each (purchase, kind, deadline) emails exactly once,
so beat can fire daily without spamming, and a postponed deadline re-arms
its reminder.

Each digest also pushes to the owner's ntfy topic, if they've set one
(issue #162) — complements the email, doesn't replace it.
"""

import datetime
import io
import logging
import traceback

from celery import shared_task
from django.contrib.auth import get_user_model
from django.core.mail import send_mail
from django.core.management import call_command
from django.template.loader import render_to_string
from django.utils import timezone

from . import bgg_sync, ntfy
from .models import Game, Purchase, ReminderLog, ToolRun

logger = logging.getLogger(__name__)

# A deadline enters the digest once it is at most this many days away.
REMINDER_WINDOW_DAYS = 7


def _due_reminders(today):
    """(purchase, kind, deadline) triples inside the reminder window that
    have not been emailed about yet."""
    horizon = today + datetime.timedelta(days=REMINDER_WINDOW_DAYS)

    # Pledge managers closing soon: the form still needs filling out
    # (not_yet covers "announced with a close date, not sent to backers
    # yet"). Dead purchases don't need their pledge manager.
    closing = (
        Purchase.objects.filter(
            pledge_manager_close_date__range=(today, horizon),
            pledge_manager_status__in=(
                Purchase.PledgeManagerStatus.NOT_YET,
                Purchase.PledgeManagerStatus.SENT_OUT,
            ),
        )
        .exclude(
            status__in=(
                Purchase.Status.PASSED,
                Purchase.Status.REFUNDED,
                Purchase.Status.NEVER_DELIVERED,
            ),
        )
        .select_related("owner")
    )
    # Campaigns ending soon: watched-but-unbacked only (DESIGN §6/§11) —
    # back it before the campaign ends or let it go.
    ending = Purchase.objects.filter(
        status=Purchase.Status.WATCHING,
        campaign_end_date__range=(today, horizon),
    ).select_related("owner")

    candidates = [
        (p, ReminderLog.Kind.PLEDGE_MANAGER, p.pledge_manager_close_date)
        for p in closing
    ] + [
        (p, ReminderLog.Kind.CAMPAIGN_END, p.campaign_end_date)
        for p in ending
    ]
    sent = set(
        ReminderLog.objects.filter(
            purchase_id__in={p.pk for p, _, _ in candidates},
        ).values_list("purchase_id", "kind", "deadline")
    )
    # TextChoices members hash/compare as their str value, so the tuples
    # match the values_list rows directly.
    return [c for c in candidates if (c[0].pk, c[1], c[2]) not in sent]


@shared_task
def send_reminder_emails():
    """Daily beat task: one digest email per owner with fresh reminders."""
    today = timezone.localdate()
    due = _due_reminders(today)

    by_owner = {}
    for purchase, kind, deadline in due:
        by_owner.setdefault(purchase.owner, []).append((purchase, kind, deadline))

    emails_sent = reminders_sent = 0
    for owner, items in by_owner.items():
        if not owner.email:
            logger.warning(
                "Skipping %d reminder(s) for %s: no email address set.",
                len(items), owner.username,
            )
            continue
        closing = [p for p, kind, _ in items if kind == ReminderLog.Kind.PLEDGE_MANAGER]
        ending = [p for p, kind, _ in items if kind == ReminderLog.Kind.CAMPAIGN_END]
        count = len(items)
        subject = (
            f"GameKeeper: {count} deadline{'s' if count > 1 else ''} "
            f"in the next {REMINDER_WINDOW_DAYS} days"
        )
        body = render_to_string("email/reminder_digest.txt", {
            "owner": owner,
            "closing": closing,
            "ending": ending,
            "window_days": REMINDER_WINDOW_DAYS,
        })
        # from_email=None -> DEFAULT_FROM_EMAIL (Purelymail in prod, §2).
        send_mail(subject, body, None, [owner.email])
        membership = getattr(owner, "membership", None)
        if membership and membership.ntfy_topic:
            ntfy.send_ntfy(membership.ntfy_topic, subject, body)
        ReminderLog.objects.bulk_create([
            ReminderLog(purchase=purchase, kind=kind, deadline=deadline)
            for purchase, kind, deadline in items
        ])
        emails_sent += 1
        reminders_sent += count

    summary = f"Sent {emails_sent} email(s) covering {reminders_sent} reminder(s)."
    logger.info(summary)
    return summary


@shared_task
def run_tool_command(run_id):
    """Run a maintenance command for the Tools page (issue #90) off-request.

    Loads the ToolRun the view created in `running` state, invokes the matching
    management command with its output captured into the row, and marks the run
    success/failed. BGG sync is scoped to the user who triggered it (the bulk
    sync reconciles that owner's Copies); cover download is app-wide. Never
    lets an exception escape unrecorded — a stuck `running` row would wedge the
    overlap guard, so failures land as status=failed with the traceback."""
    run = ToolRun.objects.get(pk=run_id)
    buf = io.StringIO()
    try:
        if run.kind == ToolRun.Kind.BGG_SYNC:
            username = run.triggered_by.username if run.triggered_by else None
            call_command("sync_bgg", user=username, stdout=buf, stderr=buf)
        elif run.kind == ToolRun.Kind.GENERATE_PREVIEWS:
            # force=True rebuilds every preview (the default only fills missing
            # ones); this action is the repair path after a preview-size or
            # generation-logic change (issue #112).
            call_command(
                "generate_cover_previews", force=True, stdout=buf, stderr=buf,
            )
        else:
            call_command("download_covers", stdout=buf, stderr=buf)
        run.summary = buf.getvalue()
        run.status = ToolRun.Status.SUCCESS
    except Exception:
        run.summary = (buf.getvalue() + "\n" + traceback.format_exc()).strip()
        run.status = ToolRun.Status.FAILED
        logger.exception("Tool run %s (%s) failed", run.pk, run.kind)
    run.finished_at = timezone.now()
    run.save(update_fields=["status", "summary", "finished_at"])
    return run.summary


@shared_task
def push_bgg_status_task(game_id, new_status, user_id, priority=None):
    """Off-request BGG collection-status push (issue #117). Views enqueue
    this instead of calling bgg_sync.push_bgg_status inline, so archiving,
    adding, or wishlisting a copy never blocks the request on a live BGG
    round-trip (geekcollection.php may retry-with-backoff like the read
    endpoints do). Thin wrapper — push_bgg_status already never raises."""
    game = Game.objects.get(pk=game_id)
    user = get_user_model().objects.get(pk=user_id) if user_id else None
    bgg_sync.push_bgg_status(game, new_status, priority=priority, user=user)


@shared_task
def push_bgg_fortrade_task(game_id, fortrade, user_id):
    """Off-request BGG "for trade" push (issue #82) — same rationale as
    push_bgg_status_task, kept as a separate task since it drives the
    merge-based push_bgg_fortrade rather than the replace-based
    push_bgg_status."""
    game = Game.objects.get(pk=game_id)
    user = get_user_model().objects.get(pk=user_id) if user_id else None
    bgg_sync.push_bgg_fortrade(game, fortrade, user=user)
