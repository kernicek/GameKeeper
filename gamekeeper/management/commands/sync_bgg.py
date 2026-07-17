"""Token-less BGG sync engine (DESIGN §8): backfill Game stats/images from
the owner's BGG collection and reconcile collection membership.

Two passes over live BGG, all network first, then one transaction:

  1. collection?stats=1 once per membership status — own=1, preordered=1
     and prevowned=1 (BGG ANDs combined status filters, so the union takes
     three requests; each retries the 202 queue). Together they are the
     authoritative membership list. Items are matched to Games by their
     PRIMARY BggLink id, never by title (§8: the primary id drives synced
     stats/image). Matched games get name/year/images/players/playtime/
     rank/rating and the owner's play count (numplays) backfilled, plus
     bgg_collection_status (own beats preordered beats previously-owned when
     several flags are set). Previously-owned games stay in the app; the UI
     marks them. A fourth request (wishlist=1) rides the same token-free
     payload but is aspirational, not membership: it is applied ONLY to
     games already in the app (status -> wishlist, membership wins) and
     never suggested for adding — a wishlist is not a collection to mirror.
  2. thing?stats=1 in polite serial batches for EVERY primary BGG id in the
     database — this adds weight (absent from the collection payload), the
     expansion->base links for Game.expands (issue #40; add-only, from the
     payload's inbound boardgameexpansion links, resolved against the
     primary BggLink ids), mechanic tags (Tag(kind=mechanic), DESIGN §10;
     fully BGG-driven, so this pass both adds and removes to stay in sync —
     see sync_mechanic_tags) and covers games not in the BGG collection yet
     (preorder imports). DESIGN §15's open question is CLOSED (probed live
     2026-07-03): /thing is gated by app registration, not login, so it
     401s without a registered-app Bearer token; with one configured (as of
     2026-07-10) the pass runs fully. Without a token it still runs (one
     request confirms the 401) and degrades to collection-only data with a
     note on the first 401.

Only the §8 BGG-synced fields + last_synced_at are ever written. App-only
data (Game.name, curation, sleeves, purchases, notes) is untouchable.
BGG values overwrite previous BGG values — BGG is the source of truth for
its own fields — so the sync is idempotent: a re-run with unchanged BGG
data reports every game as unchanged.

Reconciliation (§8: notify, never auto-remove): the report lists BGG items
with no matching Game (suggest adding), the user's active Copies whose
game is absent from BGG (cross-referenced against §6 purchase products
before flagging — a pending wave usually explains it), and the reverse
diff — Copies archived in the app (§4) whose game BGG still lists as
owned/preordered. XML API2 has no collection-write operation, so that
last one is always a fix-by-hand-on-BGG nudge. Each diff also persists
as a per-owner BggSyncDiff row feeding the §11 dashboard widget:
dismissed rows stay dismissed while the diff is still observed, resolved
diffs are deleted, and a reappearing diff is a new unreviewed row.

Credentials come from BGG_USERNAME/BGG_PASSWORD in .env (settings) and are
never logged. --dry-run still talks to live BGG (that is the probe) but
rolls the transaction back.
"""

import time
from collections import defaultdict

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from gamekeeper.bgg import (
    BggAuthError, BggError, parse_collection,
    parse_collection_status_flags, parse_things,
)
from gamekeeper.bgg_sync import (
    BGG_SYNCED_FIELDS, COLLECTION_STATUSES, apply_bgg_fields,
    bgg_credentials_error, clear_push_failure, fetch_plays,
    link_expansion_bases, make_bgg_client, push_is_pending, store_plays,
    sync_mechanic_tags,
)
from gamekeeper.models import BggLink, BggSyncDiff, Copy, Game, Product

# BGG_SYNCED_FIELDS and COLLECTION_STATUSES live in bgg_sync (shared with the
# per-game refresh, issue #44); imported above so there is one source of truth.

THING_BATCH_SIZE = 20
THING_PAUSE_SECONDS = 2.0

DECISION_NOTES = [
    "Token-less auth: own-collection downloads are exempt from BGG app "
    "registration, so the sync logs in with BGG_USERNAME/BGG_PASSWORD "
    "session cookies. The registered-app Bearer token (§8) replaces only "
    "the login step when granted.",
    "Items match Games by primary BggLink id only, never by title (§8); "
    "secondary ids are annotated in the reconciliation, not synced.",
    "BGG values overwrite previously synced BGG values (BGG is the source "
    "of truth for its own fields); app-only fields are never written.",
    "BGG's unknown-markers (0, N/A, Not Ranked) are stored as NULL.",
    "The thing pass covers ALL primary BGG ids, so preorder-imported games "
    "not in the BGG collection would get stats/covers too — but /thing is "
    "confirmed 401 over a session (§15 closed 2026-07-03: gated by app "
    "registration, not login), so this waits for the Bearer token.",
    "Play counts (numplays) sync from the token-free collection payload; 0 "
    "plays is stored as NULL, so only a positive count surfaces in the UI.",
    "Wishlist rides the same payload but is aspirational: applied only to "
    "games already in the app (membership statuses win), and wishlist-only "
    "BGG items are never suggested for adding.",
    "Collection membership is the union of own=1, preordered=1 and "
    "prevowned=1 (three requests — BGG ANDs combined status filters). The "
    "status lands in Game.bgg_collection_status with own > preordered > "
    "prev_owned precedence; previously-owned games are kept and marked, "
    "never removed.",
    "Archive mapping (§4): Copy.archive_status is app-truth for 'do I "
    "still have it'; the BGG status is evidence, never authority. The "
    "report flags both directions (prev-owned with an active Copy, "
    "archived with BGG still saying own/preordered) — XML API2 cannot "
    "write collections, so the archived-side fix is manual on BGG.",
    "Expansion->base links (Game.expands, issue #40) sync from the thing "
    "payload's inbound boardgameexpansion links, ADD-ONLY: bases already "
    "in the DB are linked, hand-set links are never removed. The thing "
    "pass covers every primary id, so already-synced expansions backfill "
    "on the first run with /thing access — which today means the Bearer "
    "token (the links degrade with the same 401 as weight). Until then "
    "sync_expansion_links backfills them by hand from the undocumented "
    "geekitems JSON — kept OUT of this scheduled sync so the unsupported "
    "endpoint cannot get the account blocked.",
    "Sync diffs persist as BggSyncDiff rows upserted on (owner, category, "
    "bgg_id) inside the write transaction (dry-run rolls them back). "
    "Dismiss is per-user 'not interested, never nag again' (§8): "
    "re-observation keeps dismissed_at; but a diff that RESOLVES is "
    "deleted, so a later reappearance is a new occurrence and nags again. "
    "Ambiguous items (no usable primary BGG id) stay report-only.",
]

DEFERRED_NOTES = [
    "Play COUNTS, wishlist STATUS and the plays HISTORY/log now sync (issue "
    "#65); wishlist browsing UI and new-expansion tracking (§8) are still "
    "later sessions. Enriching plays from a BG Stats export is the (b) fallback.",
    "Per-user BGG usernames: --bgg-username / BGG_USERNAME is the seam; a "
    "profile field comes with multi-user syncing.",
    "Celery beat weekly schedule (§8) wraps this command once Redis runs; "
    "manual 'refresh this game' button comes with the game detail page.",
    "Mechanics/categories from the thing payload wait for the §10 Tag "
    "model. (Expansion links no longer wait — issue #40 syncs them from "
    "the same payload, though live data needs the Bearer token.)",
    "thing?versions=1 card-size pre-fill (§5/§15): probe once /thing "
    "access is confirmed.",
]


class Command(BaseCommand):
    help = "Sync stats/images from BGG and reconcile collection membership (DESIGN §8)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--user", required=True,
            help="App username whose Copies drive the reconciliation.",
        )
        parser.add_argument(
            "--bgg-username", default=None,
            help="BGG account whose collection to pull (default: BGG_USERNAME).",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Fetch from live BGG and report, but write nothing to the database.",
        )

    def handle(self, *args, **options):
        User = get_user_model()
        try:
            user = User.objects.get(username=options["user"])
        except User.DoesNotExist:
            raise CommandError(f"User {options['user']!r} does not exist.")

        # Resolve BGG creds for this user (issue #118): their stored per-user
        # account, env as fallback. Guard + collection username follow suit.
        if error := bgg_credentials_error(user):
            raise CommandError(error)
        bgg_username = (
            options["bgg_username"]
            or getattr(getattr(user, "membership", None), "bgg_username", "")
            or getattr(settings, "BGG_USERNAME", "")
        )

        self.counts = {}
        self.unmatched_bgg = []      # (bgg_id, name, note) — on BGG, not in app
        self.missing_from_bgg = []   # (game, bgg_id, note) — owned Copy, not on BGG
        self.prev_owned_active = []  # games with an active Copy but prevowned on BGG
        self.archived_on_bgg = []    # (game, status) — archived Copy, BGG still own/preordered
        self.ambiguous = []          # (where, note)
        self.things_blocked = None   # message once /thing answers 401
        self.plays_blocked = None    # message once /plays answers 401 (§8)
        self._changed = defaultdict(set)  # game pk -> changed field names
        self._now = timezone.now()

        # --- Network phase (kept outside the transaction: SQLite writer lock
        # must not sit across ~20 polite serial requests) -----------------
        client = make_bgg_client(user)
        try:
            client.login()
        except BggAuthError as error:
            raise CommandError(str(error))
        self.stdout.write("Logged into BGG.")

        # Union of the three membership statuses; request order is the
        # precedence order, so setdefault keeps own over preordered over
        # previously-owned for items carrying several flags.
        collection = {}
        status_by_id = {}
        # Wishlist priority rides every payload's full <status> element (the
        # same value wherever the item appears) — no extra requests needed.
        priority_by_id = {}
        for param, status in COLLECTION_STATUSES:
            try:
                xml = client.get_collection(bgg_username, status=param)
            except BggError as error:
                raise CommandError(str(error))
            items = parse_collection(xml)
            self.counts[f"collection items ({param}=1)"] = len(items)
            self.stdout.write(f"Fetched {param}=1 collection of {bgg_username!r}: {len(items)} items.")
            for bgg_id, data in items.items():
                collection.setdefault(bgg_id, data)
                status_by_id.setdefault(bgg_id, status)
            for bgg_id, item_flags in parse_collection_status_flags(xml).items():
                priority_by_id.setdefault(bgg_id, item_flags.get("wishlist_priority"))

        # Wishlist rides in the same token-free payload but is aspirational,
        # not membership — kept OUT of the `collection` union (so it never
        # feeds the suggest-adding/missing reconciliation) and applied later
        # only to games already in the app.
        try:
            wishlist_xml = client.get_collection(bgg_username, status="wishlist")
        except BggError as error:
            raise CommandError(str(error))
        wishlist = parse_collection(wishlist_xml)
        for bgg_id, item_flags in parse_collection_status_flags(wishlist_xml).items():
            priority_by_id.setdefault(bgg_id, item_flags.get("wishlist_priority"))
        self.counts["collection items (wishlist=1)"] = len(wishlist)
        self.stdout.write(f"Fetched wishlist=1 collection of {bgg_username!r}: {len(wishlist)} items.")

        games_by_bgg_id = defaultdict(list)
        for link in BggLink.objects.filter(is_primary=True).select_related("game"):
            games_by_bgg_id[link.bgg_id].append(link.game)
        secondary_games = defaultdict(list)
        for link in BggLink.objects.filter(is_primary=False).select_related("game"):
            secondary_games[link.bgg_id].append(link.game)

        thing_data = self._fetch_things(client, sorted(games_by_bgg_id))

        # Plays history (§8): page through the owner's plays (read-only). Kept in
        # the network phase like the others; degrades on a 401 (private plays need
        # the Bearer token) rather than failing the whole sync, mirroring /thing.
        plays = []
        try:
            plays = fetch_plays(client, bgg_username)
            self.counts["plays fetched from BGG"] = len(plays)
            self.stdout.write(f"Fetched {len(plays)} plays of {bgg_username!r}.")
        except BggAuthError as error:
            self.plays_blocked = f"plays pass blocked: {error}"
        except BggError as error:
            self.plays_blocked = f"plays pass failed: {error}"

        # --- Write phase ---------------------------------------------------
        with transaction.atomic():
            for bgg_id, data in sorted(collection.items()):
                games = games_by_bgg_id.get(bgg_id)
                if not games:
                    note = ""
                    if bgg_id in secondary_games:
                        names = ", ".join(g.name for g in secondary_games[bgg_id])
                        note = f"is a secondary BGG id of: {names}"
                    self.unmatched_bgg.append((bgg_id, data["bgg_name"], note))
                    continue
                if len(games) > 1:
                    self.ambiguous.append((
                        f"BGG id {bgg_id}",
                        f"{len(games)} games share this primary id — all synced",
                    ))
                for game in games:
                    self._apply(game, {
                        **data, "bgg_collection_status": status_by_id[bgg_id],
                        "bgg_wishlist_priority": priority_by_id.get(bgg_id),
                    })
                    self._bump("games synced from collection")

            for bgg_id, data in sorted(thing_data.items()):
                # Neither is a Game field: base-game ids for the expands M2M
                # (issue #40) and mechanic names for the Tag(kind=mechanic)
                # reconcile (DESIGN §10) — _apply must never see either.
                expands_bgg_ids = data.pop("expands_bgg_ids", ())
                mechanic_names = data.pop("mechanics", ())
                for game in games_by_bgg_id.get(bgg_id, []):
                    if bgg_id in collection:
                        # Collection already carried everything but weight.
                        if "weight" in data:
                            self._apply(game, {"weight": data["weight"]})
                    else:
                        self._apply(game, data)
                        self._bump("games synced from thing only (not in BGG collection)")
                    self._link_expansion_bases(game, expands_bgg_ids, games_by_bgg_id)
                    self._sync_mechanic_tags(game, mechanic_names)

            # Wishlist: mirror onto matched games only. Membership wins — a
            # game already carrying own/preordered/prevowned keeps it; the
            # WISHLIST id is recorded in status_by_id so the stale-clear pass
            # below does not wipe it. Wishlist-only BGG items (no matching
            # Game) are deliberately NOT suggested for adding (§8).
            for bgg_id, data in sorted(wishlist.items()):
                if bgg_id in status_by_id:
                    continue
                games = games_by_bgg_id.get(bgg_id)
                if not games:
                    continue
                status_by_id[bgg_id] = Game.BggCollectionStatus.WISHLIST
                for game in games:
                    self._apply(game, {
                        **data, "bgg_collection_status": Game.BggCollectionStatus.WISHLIST,
                        "bgg_wishlist_priority": priority_by_id.get(bgg_id),
                    })
                    self._bump("games synced from wishlist (already in the app)")

            # A stale status means the game left the BGG collection entirely
            # (e.g. prevowned mark removed) — clear it, but leave games BGG
            # never covered untouched (no phantom last_synced_at). Skip a game
            # whose push_bgg_status write is still within its confirmation
            # window (issue #117) — BGG's export may not have caught up yet;
            # _reconcile_pending_pushes below is what actually resolves it.
            for bgg_id, games in games_by_bgg_id.items():
                if bgg_id in status_by_id:
                    continue
                for game in games:
                    if push_is_pending(game, self._now):
                        continue
                    if game.bgg_collection_status or game.bgg_wishlist_priority is not None:
                        self._apply(game, {"bgg_collection_status": "",
                                           "bgg_wishlist_priority": None})
                        self._bump("games whose BGG collection status was cleared")

            self._reconcile_pending_pushes(user, games_by_bgg_id, status_by_id)

            # Plays history: upsert what the network phase fetched, joined to
            # Games by primary BggLink (§8). Idempotent; plays for games not in
            # the app are skipped.
            if plays:
                play_counts = store_plays(plays, games_by_bgg_id, self._now)
                self.counts["plays synced"] = play_counts["synced"]
                self.counts["plays skipped (no matching game)"] = play_counts["skipped"]

            self._summarize_changes()
            self._reconcile(user, collection, status_by_id, games_by_bgg_id)
            self._persist_diffs(user)
            self.counts["games with cover art after sync"] = (
                Game.objects.exclude(image_url="").count()
            )
            if options["dry_run"]:
                transaction.set_rollback(True)

        self._print_report(dry_run=options["dry_run"])

    # --- thing pass ----------------------------------------------------------

    def _fetch_things(self, client, bgg_ids):
        """Batched thing?stats=1 for every primary id — serial, with a pause
        between requests. A 401 anywhere stops the pass — with a registered-
        app Bearer token configured this means a genuine auth failure, not
        the token being absent (§15)."""
        thing_data = {}
        batches = [
            bgg_ids[i:i + THING_BATCH_SIZE]
            for i in range(0, len(bgg_ids), THING_BATCH_SIZE)
        ]
        for index, batch in enumerate(batches):
            if index:
                time.sleep(THING_PAUSE_SECONDS)
            try:
                thing_data.update(parse_things(client.get_things(batch)))
            except BggAuthError as error:
                self.things_blocked = (
                    f"{error} The logged-in session is not allowed on /thing "
                    "(DESIGN §15) — weight, mechanics and stats for games "
                    "outside the BGG collection stay blocked for this run. "
                    "Confirm BGG_API_TOKEN is set to a valid registered-app "
                    "Bearer token."
                )
                break
            except BggError as error:
                self.things_blocked = f"thing pass aborted at batch {index + 1}: {error}"
                break
            self.stdout.write(f"  thing batch {index + 1}/{len(batches)} fetched.")
        self.counts["thing items fetched"] = len(thing_data)
        return thing_data

    # --- backfill --------------------------------------------------------------

    def _apply(self, game, data):
        """Write BGG-synced fields only (shared helper), recording what changed
        so the report can prove idempotency."""
        changed = apply_bgg_fields(game, data, self._now)
        self._changed[game.pk].update(changed)

    def _link_expansion_bases(self, game, base_bgg_ids, games_by_bgg_id):
        """Add-only Game.expands links from the thing payload (issue #40),
        via the shared helper; keep the per-link report counter."""
        added = link_expansion_bases(game, base_bgg_ids, games_by_bgg_id)
        if added:
            self._changed[game.pk].add("expands")
            for _ in range(added):
                self._bump("expansion links added (Game.expands)")

    def _sync_mechanic_tags(self, game, mechanic_names):
        """Reconcile Tag(kind=mechanic) GameTags from the thing payload
        (DESIGN §10), via the shared helper; keep per-tag report counters."""
        added, removed = sync_mechanic_tags(game, mechanic_names)
        if added or removed:
            self._changed[game.pk].add("mechanics")
            for _ in range(added):
                self._bump("mechanic tags added")
            for _ in range(removed):
                self._bump("mechanic tags removed")

    def _summarize_changes(self):
        synced = len(self._changed)
        updated = sum(1 for fields in self._changed.values() if fields)
        self.counts["games synced total"] = synced
        self.counts["games updated"] = updated
        self.counts["games unchanged"] = synced - updated
        for field, label in (
            ("image_url", "games that gained/changed cover art"),
            ("weight", "games that gained/changed weight"),
            ("mechanics", "games that gained/changed mechanic tags"),
        ):
            self.counts[label] = sum(
                1 for fields in self._changed.values() if field in fields
            )

    # --- write-back reconciliation (issue #117) -------------------------------

    def _reconcile_pending_pushes(self, user, games_by_bgg_id, status_by_id):
        """Resolve every Game with a live push_bgg_status marker against this
        run's read. Confirmed (read matches what we pushed) clears the marker
        and any PUSH_FAILED diff. Still pending and disagreeing (BGG's export
        hasn't caught up) re-asserts the pushed status locally and keeps the
        marker — self.pending_push_pks records these so _reconcile below
        doesn't flag an in-flight write as a stale diff. Window expired:
        clears the marker and lets the read stand."""
        self.pending_push_pks = set()
        primary_id_by_game = {
            game.pk: bgg_id
            for bgg_id, games in games_by_bgg_id.items()
            for game in games
        }
        for game in Game.objects.exclude(bgg_status_pushed_at__isnull=True):
            bgg_id = primary_id_by_game.get(game.pk)
            observed = status_by_id.get(bgg_id, "") if bgg_id is not None else ""
            if observed == game.bgg_status_pushed:
                game.bgg_status_pushed = ""
                game.bgg_status_pushed_at = None
                game.save(update_fields=["bgg_status_pushed", "bgg_status_pushed_at"])
                clear_push_failure(game, user)
                self._bump("pending BGG pushes confirmed")
            elif push_is_pending(game, self._now):
                if game.bgg_collection_status != game.bgg_status_pushed:
                    game.bgg_collection_status = game.bgg_status_pushed
                    game.save(update_fields=["bgg_collection_status"])
                self.pending_push_pks.add(game.pk)
                self._bump("BGG pushes still awaiting confirmation")
            else:
                game.bgg_status_pushed = ""
                game.bgg_status_pushed_at = None
                game.save(update_fields=["bgg_status_pushed", "bgg_status_pushed_at"])
                self._bump("BGG pushes that expired unconfirmed")

    # --- reconciliation (§8: notify, never auto-remove) -----------------------

    def _reconcile(self, user, collection, status_by_id, games_by_bgg_id):
        primary_id_by_game = {
            game.pk: bgg_id
            for bgg_id, games in games_by_bgg_id.items()
            for game in games
        }
        self._primary_id_by_game = primary_id_by_game
        owned_games = {}
        for copy in Copy.objects.filter(
            owner=user, archive_status=Copy.ArchiveStatus.ACTIVE,
        ).select_related("edition__game"):
            owned_games[copy.edition.game.pk] = copy.edition.game

        missing = []
        for game in owned_games.values():
            if game.pk in self.pending_push_pks:
                continue  # issue #117: in-flight write, not a real diff yet
            bgg_id = primary_id_by_game.get(game.pk)
            if bgg_id is None:
                self.ambiguous.append(
                    (game.name, "owned Copy but the Game has no primary BGG link"),
                )
                continue
            if bgg_id not in collection:
                missing.append((game, bgg_id))
            elif status_by_id[bgg_id] == Game.BggCollectionStatus.PREV_OWNED:
                # An active Copy that BGG says already left the collection —
                # kept in the app (never auto-removed), marked in the UI.
                self.prev_owned_active.append(game)

        # Reverse diff (§4 archive mapping): copies archived in the app
        # whose game BGG still lists as owned/preordered. Games that ALSO
        # have an active Copy are consistent (rebuy/upgrade keeps them
        # owned) and stay unflagged; archived games without a primary BGG
        # link cannot be checked and are skipped. Fix is manual on BGG —
        # XML API2 has no collection-write operation.
        archived_games = {}
        for copy in Copy.objects.filter(
            owner=user, archive_status=Copy.ArchiveStatus.ARCHIVED,
        ).select_related("edition__game"):
            game = copy.edition.game
            if game.pk not in owned_games:
                archived_games[game.pk] = game
        for game in sorted(archived_games.values(), key=lambda g: g.name):
            if game.pk in self.pending_push_pks:
                continue  # issue #117: in-flight write, not a real diff yet
            bgg_id = primary_id_by_game.get(game.pk)
            status = status_by_id.get(bgg_id)
            if status in (
                Game.BggCollectionStatus.OWN, Game.BggCollectionStatus.PREORDERED,
            ):
                self.archived_on_bgg.append((game, status))

        # Cross-reference §6 purchases before flagging: a product on a
        # non-terminal wave usually explains why BGG doesn't have it yet.
        purchase_notes = defaultdict(list)
        for product in Product.objects.filter(
            game__in=[game for game, _ in missing],
        ).select_related("wave__purchase"):
            purchase_notes[product.game_id].append(
                f"{product.wave.purchase.name} "
                f"(wave {product.wave.number}: {product.wave.get_status_display()})"
            )
        for game, bgg_id in missing:
            notes = purchase_notes.get(game.pk)
            note = (
                "linked purchase: " + "; ".join(notes) if notes
                else "no linked purchase — review (culled on BGG, or never added?)"
            )
            self.missing_from_bgg.append((game, bgg_id, note))

        self.counts["your active copies (games)"] = len(owned_games)
        self.counts["owned games missing from BGG"] = len(self.missing_from_bgg)
        self.counts["active copies marked previously-owned on BGG"] = len(self.prev_owned_active)
        self.counts["archived copies still owned on BGG"] = len(self.archived_on_bgg)
        self.counts["BGG items not in the app"] = len(self.unmatched_bgg)

    def _persist_diffs(self, user):
        """Persist the four reconciliation lists as per-owner BggSyncDiff rows
        (§11 dashboard widget). Upsert keyed (owner, category, bgg_id): new
        diffs arrive unreviewed, existing rows keep dismissed_at (§8:
        dismissed means never nag again) while note/name/last_seen_at
        refresh. Rows no longer observed this run are deleted — the diff
        resolved; if it ever comes back that is a new occurrence and nags
        again. Runs inside the write transaction, so --dry-run rolls the
        rows back too."""
        Category = BggSyncDiff.Category
        observed = []
        for bgg_id, name, note in self.unmatched_bgg:
            observed.append((Category.SUGGEST_ADD, bgg_id, None, name, note))
        for game, bgg_id, note in self.missing_from_bgg:
            observed.append((Category.MISSING_FROM_BGG, bgg_id, game, "", note))
        for game in self.prev_owned_active:
            observed.append((
                Category.PREV_OWNED_ACTIVE, self._primary_id_by_game[game.pk],
                game, "", "",
            ))
        for game, status in self.archived_on_bgg:
            observed.append((
                Category.ARCHIVED_ON_BGG, self._primary_id_by_game[game.pk],
                game, "",
                f"BGG still says {status.label.lower()} — fix by hand on BGG",
            ))

        seen_pks = []
        for category, bgg_id, game, bgg_name, note in observed:
            diff, created = BggSyncDiff.objects.update_or_create(
                owner=user, category=category, bgg_id=bgg_id,
                defaults={
                    "game": game, "bgg_name": bgg_name, "note": note,
                    "last_seen_at": self._now,
                },
            )
            seen_pks.append(diff.pk)
            if created:
                self._bump("sync diffs recorded (new, unreviewed)")
        # Scoped to the categories THIS command manages: PUSH_FAILED (issue
        # #117) and NEW_EXPANSION are written by other code paths
        # (push_bgg_status / the new-expansion widget) and must survive a
        # sync run even though this pass never observes them.
        managed_categories = (
            Category.SUGGEST_ADD, Category.MISSING_FROM_BGG,
            Category.PREV_OWNED_ACTIVE, Category.ARCHIVED_ON_BGG,
        )
        resolved, _ = (
            BggSyncDiff.objects.filter(owner=user, category__in=managed_categories)
            .exclude(pk__in=seen_pks).delete()
        )
        self.counts["sync diffs open after run"] = len(seen_pks)
        self.counts["sync diffs resolved (removed)"] = resolved

    # --- report ----------------------------------------------------------------

    def _print_report(self, dry_run):
        write = self.stdout.write
        if dry_run:
            write(self.style.WARNING("DRY RUN — nothing was written to the database.\n"))

        write(self.style.MIGRATE_HEADING("Summary"))
        for key in sorted(self.counts):
            write(f"  {key}: {self.counts[key]}")

        write(self.style.MIGRATE_HEADING("On BGG but not in the app (suggest adding — never auto-added)"))
        if self.unmatched_bgg:
            for bgg_id, name, note in self.unmatched_bgg:
                suffix = f" — {note}" if note else ""
                write(f"  BGG {bgg_id} ({name!r}){suffix}")
        else:
            write("  none")

        write(self.style.MIGRATE_HEADING("In the app but missing from BGG (flagged for review — never auto-removed)"))
        if self.missing_from_bgg:
            for game, bgg_id, note in self.missing_from_bgg:
                write(f"  {game.name!r} (BGG {bgg_id}): {note}")
        else:
            write("  none")

        write(self.style.MIGRATE_HEADING("Active copies marked previously-owned on BGG (kept — the UI shows the mark)"))
        if self.prev_owned_active:
            for game in self.prev_owned_active:
                write(f"  {game.name!r}")
        else:
            write("  none")

        write(self.style.MIGRATE_HEADING("Archived in the app but still owned on BGG (update BGG by hand — the API cannot write)"))
        if self.archived_on_bgg:
            for game, status in self.archived_on_bgg:
                write(f"  {game.name!r}: BGG says {status.label.lower()}")
        else:
            write("  none")

        write(self.style.MIGRATE_HEADING("Ambiguous"))
        if self.ambiguous:
            for where, note in self.ambiguous:
                write(f"  {where}: {note}")
        else:
            write("  none")

        if self.things_blocked:
            write(self.style.MIGRATE_HEADING("Thing pass blocked"))
            write(f"  {self.things_blocked}")

        if self.plays_blocked:
            write(self.style.MIGRATE_HEADING("Plays pass blocked"))
            write(f"  {self.plays_blocked}")

        write(self.style.MIGRATE_HEADING("Interpretation decisions"))
        for note in DECISION_NOTES:
            write(f"  - {note}")

        write(self.style.MIGRATE_HEADING("Deferred (not synced)"))
        for note in DEFERRED_NOTES:
            write(f"  - {note}")

        write(self.style.SUCCESS("Done." if not dry_run else "Dry run complete."))

    def _bump(self, key):
        self.counts[key] = self.counts.get(key, 0) + 1
