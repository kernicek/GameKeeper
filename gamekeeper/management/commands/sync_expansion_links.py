"""Manual Game.expands backfill via BGG's undocumented geekitems JSON
(issue #40 stopgap).

The proper source for expansion->base links is the /thing payload, and
sync_bgg already consumes it — but /thing is 401-gated token-less (DESIGN
§15, closed 2026-07-03), so that path stays dormant until the Bearer token.
/api/geekitems — the JSON endpoint BGG's own frontend uses — carries the
same links (links.expandsboardgame) and answers 200 with no auth at all.

This command is DELIBERATELY separate from sync_bgg (user decision
2026-07-03): the endpoint is unsupported, and hitting it from a scheduled
weekly sync is how an anonymous client gets itself blocked. Run it by hand
when expansions need linking. It keeps the request volume minimal:

  - Only Games with type=expansion and a primary BggLink are fetched, and
    by default only those with NO expands links yet; --all refreshes every
    expansion (e.g. after adding a base game that was missing last run).
  - One item per request (the endpoint has no batching), strictly serial
    with a pause, and any BGG error aborts the fetch loop instead of
    retrying its way into a ban.

Linking semantics are identical to sync_bgg's thing pass: bases are
resolved by primary BggLink id, ADD-ONLY — bases missing from the DB are
reported and skipped (re-run after adding them), hand-set links are never
removed. No login and no --user: expands is structural, not per-user.
"""

import time
from collections import defaultdict

from django.core.management.base import BaseCommand
from django.db import transaction

from gamekeeper.bgg import BggClient, BggError, parse_geekitem
from gamekeeper.models import BggLink, Game

PAUSE_SECONDS = 2.0


class Command(BaseCommand):
    help = (
        "Fill Game.expands from BGG's geekitems JSON — manual stopgap while "
        "/thing is token-gated (issue #40)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--all", action="store_true",
            help="Fetch every expansion, not only those still missing expands links.",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Fetch from live BGG and report, but write nothing to the database.",
        )

    def handle(self, *args, **options):
        games_by_bgg_id = defaultdict(list)
        for link in BggLink.objects.filter(is_primary=True).select_related("game"):
            games_by_bgg_id[link.bgg_id].append(link.game)
        primary_id_by_pk = {
            game.pk: bgg_id
            for bgg_id, games in games_by_bgg_id.items()
            for game in games
        }

        expansions = Game.objects.filter(type=Game.Type.EXPANSION).order_by("name")
        if not options["all"]:
            expansions = expansions.filter(expands__isnull=True)
        expansions = list(expansions)

        no_primary_link = [g for g in expansions if g.pk not in primary_id_by_pk]
        targets = [g for g in expansions if g.pk in primary_id_by_pk]
        for game in no_primary_link:
            self.stdout.write(self.style.WARNING(
                f"  {game.name!r}: no primary BGG link — cannot fetch, skipped."
            ))
        self.stdout.write(
            f"Fetching geekitems for {len(targets)} expansion(s)"
            + ("" if options["all"] else " without expands links") + "."
        )

        # --- Network phase (anonymous — geekitems needs no login; kept
        # outside the transaction like sync_bgg's passes) -----------------
        client = BggClient("", "")
        fetched = {}  # game pk -> [base bgg ids]
        aborted = None
        for index, game in enumerate(targets):
            if index:
                time.sleep(PAUSE_SECONDS)
            try:
                data = parse_geekitem(client.get_geekitem(primary_id_by_pk[game.pk]))
            except BggError as error:
                # Politeness over completeness: stop instead of hammering an
                # endpoint that just refused us — what IS fetched still lands.
                aborted = f"stopped at {game.name!r} ({index}/{len(targets)} fetched): {error}"
                break
            fetched[game.pk] = data["expands_bgg_ids"]

        # --- Write phase -----------------------------------------------------
        linked = already = 0
        unresolved = []  # (game, base bgg id) — base not in the DB
        with transaction.atomic():
            for game in targets:
                base_ids = fetched.get(game.pk)
                if base_ids is None:
                    continue
                if not base_ids:
                    self.stdout.write(self.style.WARNING(
                        f"  {game.name!r}: BGG lists no base game — check the "
                        "BGG id (is it really an expansion?)"
                    ))
                    continue
                current = {base.pk for base in game.expands.all()}
                for base_id in base_ids:
                    bases = games_by_bgg_id.get(base_id)
                    if not bases:
                        unresolved.append((game, base_id))
                        continue
                    for base in bases:
                        if base.pk == game.pk or base.pk in current:
                            already += 1
                            continue
                        game.expands.add(base)
                        current.add(base.pk)
                        linked += 1
                        self.stdout.write(f"  {game.name!r} -> expands {base.name!r}")
            if options["dry_run"]:
                transaction.set_rollback(True)

        if unresolved:
            self.stdout.write(self.style.MIGRATE_HEADING(
                "Base games not in the app (add them, then re-run)"
            ))
            for game, base_id in unresolved:
                self.stdout.write(f"  {game.name!r} expands BGG {base_id}")

        self.stdout.write(self.style.MIGRATE_HEADING("Summary"))
        self.stdout.write(f"  expansions fetched: {len(fetched)}")
        self.stdout.write(f"  links added: {linked}")
        self.stdout.write(f"  links already present: {already}")
        self.stdout.write(f"  base games not in the app: {len(unresolved)}")
        self.stdout.write(f"  expansions without a primary BGG link: {len(no_primary_link)}")
        if aborted:
            self.stdout.write(self.style.WARNING(f"  ABORTED: {aborted}"))
        if options["dry_run"]:
            self.stdout.write(self.style.WARNING("DRY RUN — nothing was written."))
        else:
            self.stdout.write(self.style.SUCCESS("Done."))
