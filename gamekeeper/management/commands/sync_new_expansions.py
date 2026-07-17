"""Discover new expansions of owned base games via BGG's geekitems JSON
(issue #64 stopgap — the same undocumented endpoint issue #40 uses for the
opposite direction; see sync_expansion_links.py).

/thing is 401-gated token-less (DESIGN §15), so "does an owned base game
have a new expansion" rides the token-less geekitems JSON the same way
#40's stopgap does: one request per base, strictly serial with a pause,
abort (not retry) on any BGG error.

Deliberately separate from sync_bgg and NOT on the Celery beat schedule
(mirrors the #40 stopgap's 2026-07-03 decision): an anonymous client
hammering an unsupported endpoint on a schedule is how it gets blocked.
Run by hand.

A base game's first-ever expansion-sighting batch is its baseline: rows
land in ExpansionSighting but no BggSyncDiff notification fires, so
importing an established collection doesn't flag every existing expansion
as "new" (DESIGN §8's "appeared after initial sync"). A later run that
finds a bgg_id not yet in ExpansionSighting for that base is what creates
the per-owner notification, for every current active owner of the base.
"""

import time

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from gamekeeper.bgg import BggClient, BggError, parse_geekitem
from gamekeeper.models import BggSyncDiff, Copy, ExpansionSighting, Game

PAUSE_SECONDS = 2.0


class Command(BaseCommand):
    help = (
        "Discover new expansions of owned base games via BGG's geekitems "
        "JSON — manual stopgap while /thing is token-gated (issue #64)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Fetch from live BGG and report, but write nothing to the database.",
        )

    def handle(self, *args, **options):
        bases = list(
            Game.objects.filter(
                type=Game.Type.BASE,
                editions__copies__archive_status=Copy.ArchiveStatus.ACTIVE,
            ).distinct().order_by("name")
        )
        no_primary_link = [g for g in bases if g.primary_bgg_link is None]
        targets = [g for g in bases if g.primary_bgg_link is not None]
        for game in no_primary_link:
            self.stdout.write(self.style.WARNING(
                f"  {game.name!r}: no primary BGG link — cannot fetch, skipped."
            ))
        self.stdout.write(f"Fetching geekitems for {len(targets)} owned base game(s).")

        # --- Network phase (anonymous — geekitems needs no login; kept
        # outside the transaction like sync_expansion_links.py) -------------
        client = BggClient("", "")
        fetched = {}  # game pk -> [{"bgg_id", "name"}, ...]
        aborted = None
        for index, game in enumerate(targets):
            if index:
                time.sleep(PAUSE_SECONDS)
            try:
                data = parse_geekitem(client.get_geekitem(game.primary_bgg_link.bgg_id))
            except BggError as error:
                # Politeness over completeness: stop instead of hammering an
                # endpoint that just refused us — what IS fetched still lands.
                aborted = f"stopped at {game.name!r} ({index}/{len(targets)} fetched): {error}"
                break
            fetched[game.pk] = data["expansions"]

        # --- Write phase -----------------------------------------------------
        now = timezone.now()
        baselines = discovered = notified = 0
        with transaction.atomic():
            for game in targets:
                expansions = fetched.get(game.pk)
                if expansions is None:
                    continue
                is_baseline = not ExpansionSighting.objects.filter(base=game).exists()
                known_ids = set(
                    ExpansionSighting.objects.filter(base=game)
                    .values_list("bgg_id", flat=True)
                )
                new_entries = [e for e in expansions if e["bgg_id"] not in known_ids]
                if not new_entries:
                    continue
                ExpansionSighting.objects.bulk_create([
                    ExpansionSighting(base=game, bgg_id=e["bgg_id"], bgg_name=e["name"])
                    for e in new_entries
                ])
                if is_baseline:
                    baselines += len(new_entries)
                    self.stdout.write(
                        f"  {game.name!r}: seeded {len(new_entries)} baseline expansion(s)."
                    )
                    continue
                discovered += len(new_entries)
                owner_ids = list(
                    Copy.objects.filter(
                        edition__game=game, archive_status=Copy.ArchiveStatus.ACTIVE,
                    ).values_list("owner", flat=True).distinct()
                )
                for entry in new_entries:
                    for owner_id in owner_ids:
                        BggSyncDiff.objects.update_or_create(
                            owner_id=owner_id,
                            category=BggSyncDiff.Category.NEW_EXPANSION,
                            bgg_id=entry["bgg_id"],
                            defaults={
                                "bgg_name": entry["name"],
                                "note": f"New expansion for {game.name}",
                                "last_seen_at": now,
                            },
                        )
                        notified += 1
                self.stdout.write(
                    f"  {game.name!r}: {len(new_entries)} new expansion(s), "
                    f"notified {len(owner_ids)} owner(s)."
                )
            if options["dry_run"]:
                transaction.set_rollback(True)

        self.stdout.write(self.style.MIGRATE_HEADING("Summary"))
        self.stdout.write(f"  base games fetched: {len(fetched)}")
        self.stdout.write(f"  baseline expansions seeded: {baselines}")
        self.stdout.write(f"  new expansions discovered: {discovered}")
        self.stdout.write(f"  owner notifications created/updated: {notified}")
        self.stdout.write(f"  base games without a primary BGG link: {len(no_primary_link)}")
        if aborted:
            self.stdout.write(self.style.WARNING(f"  ABORTED: {aborted}"))
        if options["dry_run"]:
            self.stdout.write(self.style.WARNING("DRY RUN — nothing was written."))
        else:
            self.stdout.write(self.style.SUCCESS("Done."))
