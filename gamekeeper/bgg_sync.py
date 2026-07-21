"""Model-aware BGG sync helpers (DESIGN §8), shared by the bulk `sync_bgg`
management command and the per-game on-demand refresh (issue #44).

`bgg.py` stays model-free (pure API client + parsers); this module is the seam
where parsed BGG data meets the Game model. It owns the single source of truth
for WHICH fields a sync may write (`BGG_SYNCED_FIELDS`), the write helper that
enforces it (`apply_bgg_fields`), the add-only expansion linking
(`link_expansion_bases`, issue #40), the BGG-driven mechanic tag reconcile
(`sync_mechanic_tags`, DESIGN §10), the designer reconcile (`sync_designers`,
issue #19), the single-game engine (`sync_game`) and its create-from-id
counterpart (`create_game_from_bgg`, issue #55).

Rules never change between the bulk and per-game paths: only the §8 BGG-synced
fields plus `last_synced_at` are ever written; curated app data (name, curation,
sleeves, purchases) is untouchable; BGG values overwrite previous BGG values, so
a re-sync of unchanged data is idempotent (but still refreshes `last_synced_at`).
"""

import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import timedelta

import requests
from django.conf import settings
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from gamekeeper.bgg import (
    BggAuthError, BggClient, BggError, parse_collection,
    parse_collection_error, parse_collection_status_flags, parse_geekitem,
    parse_plays, parse_plays_error,
)
from gamekeeper.models import (
    BggLink, BggSyncDiff, Copy, Designer, Edition, Game, GameTag, Play,
    PlayPlayer, Tag,
)

# The only Game fields a sync may write (besides last_synced_at).
BGG_SYNCED_FIELDS = frozenset({
    "bgg_name", "year_published", "image_url", "thumbnail_url",
    "min_players", "max_players", "min_playtime", "max_playtime",
    "weight", "bgg_rank", "bgg_rating", "bgg_numplays",
    "bgg_collection_status", "bgg_wishlist_priority",
})

# Collection statuses synced by the BULK command, in precedence order (first
# match wins when BGG sets several flags on one item): API param -> stored
# status. The bulk sync issues one status-filtered request each.
COLLECTION_STATUSES = (
    ("own", Game.BggCollectionStatus.OWN),
    ("preordered", Game.BggCollectionStatus.PREORDERED),
    ("prevowned", Game.BggCollectionStatus.PREV_OWNED),
)

# Same precedence for the per-game refresh, which reads all flags off one
# unfiltered <status> element (issue #44) — wishlist last, membership wins.
# The four extra BGG flags (issue #81) fold into the existing vocabulary:
# for-trade means you own the copy; the want-type flags are all aspirational,
# so they map to wishlist — without these entries a re-sync would blank the
# status of a game imported from a want-only collection row.
# NOTE: this STORED-status precedence (preordered > prevowned) deliberately
# differs from the IMPORT-ACTION precedence in import_action_from_flags
# (prevowned > preordered) — do not "fix" one to match the other.
STATUS_FLAG_PRECEDENCE = (
    ("own", Game.BggCollectionStatus.OWN),
    ("fortrade", Game.BggCollectionStatus.OWN),
    ("preordered", Game.BggCollectionStatus.PREORDERED),
    ("prevowned", Game.BggCollectionStatus.PREV_OWNED),
    ("wishlist", Game.BggCollectionStatus.WISHLIST),
    ("want", Game.BggCollectionStatus.WISHLIST),
    ("wanttoplay", Game.BggCollectionStatus.WISHLIST),
    ("wanttobuy", Game.BggCollectionStatus.WISHLIST),
)

# Write-back (issue #117): how long a just-pushed status is treated as
# authoritative before a disagreeing read is allowed to win again. BGG's
# collection export can lag a website write by some unknown amount — this is
# a starting guess, not measured against live behaviour.
PUSH_CONFIRM_WINDOW = timedelta(hours=24)

# Shown whenever BGG auth isn't configured. A collection username is always
# required (whose collection to pull); auth is EITHER the registered-app Bearer
# token (§8 phase 2) OR the account password (phase 1 fallback).
CREDENTIALS_ERROR = (
    "BGG credentials are not configured — set BGG_USERNAME plus either "
    "BGG_API_TOKEN or BGG_PASSWORD in .env to sync from BGG."
)


def resolve_bgg_credentials(user=None):
    """The (username, password, token) triple for BGG auth (issue #118).

    Prefers the user's stored per-user credentials (Membership.bgg_username +
    the decrypted Membership password), falling back to the instance-wide env
    settings for bootstrap / single-user installs. `user=None` means env-only,
    preserving the pre-#118 behaviour.

    A stored per-user password opts that user into their own password login, so
    the instance Bearer token is dropped for them — otherwise login() would take
    the token path and pull under the shared app account instead of the user's."""
    username = getattr(settings, "BGG_USERNAME", "")
    password = getattr(settings, "BGG_PASSWORD", "")
    token = getattr(settings, "BGG_API_TOKEN", "")

    membership = getattr(user, "membership", None) if user is not None else None
    if membership is not None:
        if membership.bgg_username:
            username = membership.bgg_username
        stored_password = membership.get_bgg_password()
        if stored_password:
            password = stored_password
            token = ""  # the user's own password login wins over the app token
    return username, password, token


def bgg_credentials_error(user=None):
    """Non-empty CREDENTIALS_ERROR if BGG auth can't be assembled for `user`
    (env fallback when the user has none), else "". `user=None` ⇒ env-only.
    Callers own how they surface it (SyncResult.error, CommandError…)."""
    username, password, token = resolve_bgg_credentials(user)
    if not username or not (password or token):
        return CREDENTIALS_ERROR
    return ""


def make_bgg_client(user=None):
    """A BggClient built from `user`'s resolved credentials (issue #118), the
    instance env as fallback: the Bearer token when configured (unlocks /thing),
    else the username/password login (own collection only). login() picks the
    path — this just wires the credentials in."""
    username, password, token = resolve_bgg_credentials(user)
    return BggClient(username, password, token=token)


def _status_from_flags(flags):
    """Map an item's status booleans to a stored status (own > preordered >
    prevowned > wishlist); "" when none of the tracked flags are set."""
    for name, status in STATUS_FLAG_PRECEDENCE:
        if flags.get(name):
            return status
    return ""

# Field name -> human label for the per-game success message. Several fields
# fold to one label (min/max players -> "players") and dedupe in order.
FIELD_LABELS = {
    "bgg_name": "name", "year_published": "year",
    "image_url": "cover", "thumbnail_url": "thumbnail",
    "min_players": "players", "max_players": "players",
    "min_playtime": "playtime", "max_playtime": "playtime",
    "weight": "weight", "bgg_rank": "rank", "bgg_rating": "rating",
    "bgg_numplays": "plays", "bgg_collection_status": "collection status",
    "bgg_wishlist_priority": "wishlist priority",
    "expands": "expansion links",
}


def apply_bgg_fields(game, data, now):
    """Write BGG-synced fields only; return the list of fields that actually
    changed so callers can prove idempotency. `last_synced_at` is always
    stamped (even when nothing else changed), so a re-sync records the touch."""
    unexpected = set(data) - BGG_SYNCED_FIELDS
    assert not unexpected, f"refusing to write non-BGG fields: {unexpected}"
    changed = [
        field_name for field_name, value in data.items()
        if getattr(game, field_name) != value
    ]
    for field_name in changed:
        setattr(game, field_name, data[field_name])
    game.last_synced_at = now
    game.save(update_fields=[*changed, "last_synced_at"])
    return changed


def link_expansion_bases(game, base_bgg_ids, games_by_bgg_id):
    """Point an expansion's Game.expands at its base game(s) — issue #40.

    base_bgg_ids come from the thing payload's inbound boardgameexpansion
    links; only bases already in the DB (by primary BggLink) are linked.
    ADD-ONLY: expands is structural and admin-curated links (localized bases,
    fan content BGG models differently) must survive a sync, so nothing is ever
    removed. Returns the number of links added."""
    if not base_bgg_ids:
        return 0
    current = {base.pk for base in game.expands.all()}
    added = 0
    for base_bgg_id in base_bgg_ids:
        for base in games_by_bgg_id.get(base_bgg_id, []):
            if base.pk == game.pk or base.pk in current:
                continue
            game.expands.add(base)
            current.add(base.pk)
            added += 1
    return added


def sync_mechanic_tags(game, mechanic_names):
    """Reconcile game's Tag(kind=mechanic) GameTags to exactly match the
    thing payload's mechanic list (DESIGN §10). Unlike expands, mechanics
    have no admin-curated side to preserve — BGG is the sole source, so
    this both adds and removes to stay in sync. Scoped to kind=mechanic:
    themes and other GameTags are never touched. Returns (added, removed)."""
    tags = [
        Tag.objects.get_or_create(kind=Tag.Kind.MECHANIC, name=name)[0]
        for name in mechanic_names
    ]
    wanted_pks = {tag.pk for tag in tags}
    current = {
        game_tag.tag_id: game_tag
        for game_tag in game.game_tags.filter(tag__kind=Tag.Kind.MECHANIC)
    }
    added = 0
    for tag in tags:
        if tag.pk not in current:
            GameTag.objects.create(game=game, tag=tag)
            added += 1
    removed = 0
    for tag_id, game_tag in current.items():
        if tag_id not in wanted_pks:
            game_tag.delete()
            removed += 1
    return added, removed


def sync_designers(game, designers):
    """Reconcile game's Designer M2M to exactly match the thing payload's
    designer list (issue #19). Like mechanics, BGG is the sole source, so
    this both adds and removes. Unlike mechanics, designers dedupe by BGG id
    (`bgg_designer_id`), not name — the thing payload's boardgamedesigner
    links carry a stable id. `designers` is the parsed list of
    {"bgg_id", "name"} dicts from `parse_things`. Returns (added, removed)."""
    resolved = [
        Designer.objects.get_or_create(
            bgg_designer_id=entry["bgg_id"], defaults={"name": entry["name"]},
        )[0]
        for entry in designers
    ]
    wanted_pks = {designer.pk for designer in resolved}
    current = set(game.designers.values_list("pk", flat=True))
    added = 0
    for designer in resolved:
        if designer.pk not in current:
            game.designers.add(designer)
            added += 1
    removed = 0
    for pk in current - wanted_pks:
        game.designers.remove(pk)
        removed += 1
    return added, removed


def _games_by_primary_id(bgg_ids):
    """{bgg_id: [Game, ...]} for the given ids by their PRIMARY BggLink."""
    result = defaultdict(list)
    for link in (
        BggLink.objects.filter(is_primary=True, bgg_id__in=bgg_ids)
        .select_related("game")
    ):
        result[link.bgg_id].append(link.game)
    return result


@dataclass
class SyncResult:
    """Outcome of a per-game sync — the view renders this, never an exception."""
    ok: bool = False                 # a sync ran and wrote (or confirmed) data
    error: str = ""                  # fatal: creds/login/collection failure
    no_primary_link: bool = False    # game has no primary BggLink to sync by
    not_in_collection: bool = False  # BGG lists this game in no status
    changed: list = field(default_factory=list)        # raw field names
    changed_labels: list = field(default_factory=list)  # human labels, deduped
    links_note: str = ""             # geekitems (expansion links) degraded
    plays_synced: int = 0            # plays upserted for this game (§8)
    plays_note: str = ""             # plays history degraded (401 / endpoint down)
    last_synced_at: object = None


# Polite pause between /plays pages (BGG serves 100 plays/page). Matches the
# thing pass's cadence — an unregistered session has no rate allowance to burn.
PLAYS_PAGE_PAUSE_SECONDS = 2.0


def fetch_plays(client, bgg_username, *, bgg_id=None):
    """NETWORK phase for the read-only plays history (DESIGN §8): page through
    xmlapi2/plays for bgg_username and return every parsed play dict (see
    bgg.parse_plays). Deliberately write-free so the bulk command gathers plays
    OUTSIDE its write transaction (no SQLite writer lock held across serial
    requests, matching the collection/thing passes). bgg_id restricts the pull to
    one thing (the per-game refresh). Raises BggAuthError on a 401 (private plays
    need the Bearer token) and BggError on a persistent failure or <errors>
    payload — callers decide whether to degrade or abort."""
    collected = []
    page = 1
    while True:
        xml = client.get_plays(bgg_username, page=page, bgg_id=bgg_id)
        if error := parse_plays_error(xml):
            raise BggError(f"BGG /plays error: {error}")
        plays, total = parse_plays(xml)
        collected.extend(plays)
        if not plays or len(collected) >= total:
            break
        page += 1
        time.sleep(PLAYS_PAGE_PAUSE_SECONDS)
    return collected


def store_plays(plays, games_by_bgg_id, now):
    """WRITE phase for the plays history: upsert each parsed play into
    Play/PlayPlayer, joined to a Game via its primary BggLink objectid.
    Idempotent — keyed on (source=BGG, external_id), so a re-sync updates the play
    in place and replaces its players rather than duplicating. Plays whose
    objectid has no primary BggLink are skipped (no Game to attach to — same as an
    unmatched collection item). `games_by_bgg_id` maps a primary bgg_id to its
    Game(s). Returns {"synced": n, "skipped": n}."""
    synced = 0
    skipped = 0
    for play in plays:
        games = games_by_bgg_id.get(play["objectid"])
        if not games:
            skipped += 1
            continue
        for game in games:
            _store_one_play(game, play, now)
            synced += 1
    return {"synced": synced, "skipped": skipped}


def _store_one_play(game, play, now):
    """Upsert one play + replace its players. update_or_create keys on the stable
    (source, external_id) so BGG re-sends of the same play land on the same row."""
    play_obj, _ = Play.objects.update_or_create(
        source=Play.Source.BGG,
        external_id=play["external_id"],
        defaults={
            "game": game,
            "play_date": play["play_date"],
            "quantity": play["quantity"],
            "length_minutes": play["length_minutes"],
            "location": play["location"],
            "incomplete": play["incomplete"],
            "comments": play["comments"],
            "synced_at": now,
        },
    )
    play_obj.players.all().delete()
    PlayPlayer.objects.bulk_create([
        PlayPlayer(
            play=play_obj,
            name=player["name"], username=player["username"],
            score=player["score"], won=player["won"], is_new=player["is_new"],
            color=player["color"], start_position=player["start_position"],
            rating=player["rating"],
        )
        for player in play["players"]
    ])


def sync_game(game, *, now=None, client=None, user=None):
    """Refresh one Game's BGG-sourced fields on demand (issue #44).

    One id-filtered, status-less collection request settles this game: BGG
    returns its whole <status> element (own/preordered/prevowned/wishlist) plus
    every token-free field (name/year/images/players/playtime/rank/rating/
    numplays) — no full-collection download. Weight lives only on the token-
    gated /thing, so it stays blank (the detail page marks it). For expansions,
    a best-effort call to the unofficial geekitems JSON backfills the expands
    base links (issue #40) — the token-free source /thing's 401 withholds.
    Never raises: failures come back on the returned SyncResult.

    `client` (issue #81): an already-logged-in BggClient to reuse, so a bulk
    import doesn't log in once per game. The client is transport only — the
    collection username resolves from `user` (issue #118), env as fallback."""
    result = SyncResult()

    bgg_login, _, _ = resolve_bgg_credentials(user)
    if client is None and (error := bgg_credentials_error(user)):
        result.error = error
        return result

    link = game.primary_bgg_link
    if link is None:
        result.no_primary_link = True
        return result
    bgg_id = link.bgg_id
    now = now or timezone.now()

    # --- Network phase (kept outside the transaction) ---------------------
    needs_login = client is None
    if needs_login:
        client = make_bgg_client(user)
    try:
        if needs_login:
            client.login()
        xml = client.get_collection(bgg_login, status=None, bgg_id=bgg_id)
        stats = parse_collection(xml)
        flags = parse_collection_status_flags(xml)
    except BggAuthError as error:
        result.error = str(error)
        return result
    except (BggError, requests.RequestException) as error:
        result.error = f"BGG sync failed: {error}"
        return result

    # Expansion links live on the anonymous geekitems JSON — only expansions
    # expand anything, so base games skip the extra call. Best-effort: a
    # failure of the unofficial endpoint must not fail the sync.
    expands_bgg_ids = ()
    if game.type == Game.Type.EXPANSION:
        try:
            expands_bgg_ids = tuple(
                parse_geekitem(client.get_geekitem(bgg_id)).get("expands_bgg_ids", ())
            )
        except (BggError, requests.RequestException) as error:
            result.links_note = f"Expansion links skipped: {error}"

    # --- Write phase ------------------------------------------------------
    changed = set()
    applied = False
    with transaction.atomic():
        data = stats.get(bgg_id)
        if data is not None:
            item_flags = flags.get(bgg_id, {})
            changed.update(apply_bgg_fields(
                game, {
                    **data,
                    "bgg_collection_status": _status_from_flags(item_flags),
                    "bgg_wishlist_priority": item_flags.get("wishlist_priority"),
                }, now,
            ))
            applied = True
        else:
            # BGG lists this game in no status — clear a now-stale status
            # (it left the collection), else leave it untouched (no phantom
            # last_synced_at), matching the bulk sync. Unless a push_bgg_status
            # write is still within its confirmation window (issue #117):
            # BGG's export may simply not have caught up yet, so don't wipe
            # our own not-yet-confirmed write.
            result.not_in_collection = True
            if push_is_pending(game, now):
                pass
            elif game.bgg_collection_status or game.bgg_wishlist_priority is not None:
                changed.update(apply_bgg_fields(
                    game, {"bgg_collection_status": "",
                           "bgg_wishlist_priority": None}, now,
                ))
                applied = True

        if expands_bgg_ids and link_expansion_bases(
            game, expands_bgg_ids, _games_by_primary_id(expands_bgg_ids),
        ):
            changed.add("expands")
            if not applied:
                # Expansion links added but no scalar write happened — still
                # record the touch so last_synced_at reflects this sync.
                apply_bgg_fields(game, {}, now)
                applied = True

    result.ok = True
    result.changed = sorted(changed)
    result.changed_labels = list(dict.fromkeys(
        FIELD_LABELS.get(name, name) for name in result.changed
    ))
    result.last_synced_at = game.last_synced_at

    # Plays history (§8): best-effort and independent of the collection write —
    # a plays failure (401 token-less, or the endpoint down) must never fail the
    # per-game refresh. One id-filtered pull, upserted in its own small write.
    try:
        plays = fetch_plays(client, bgg_login, bgg_id=bgg_id)
        with transaction.atomic():
            result.plays_synced = store_plays(plays, {bgg_id: [game]}, now)["synced"]
    except BggAuthError:
        result.plays_note = "Plays skipped: BGG requires the Bearer token for this history."
    except (BggError, requests.RequestException) as error:
        result.plays_note = f"Plays skipped: {error}"
    return result


@dataclass
class CreateResult:
    """Outcome of create_game_from_bgg — the add-game view renders this,
    never an exception. Exactly one of game/existing/error is meaningful."""
    game: object = None      # the freshly created Game
    existing: object = None  # a Game already linked to the id; nothing created
    error: str = ""          # fatal: creds/network/not-in-collection
    sync: object = None      # the underlying SyncResult on success


def create_game_from_bgg(bgg_id, *, now=None, client=None, user=None):
    """Create a Game (plus primary BggLink) from a bare BGG id — issue #55,
    the creation counterpart of sync_game.

    The game is born empty and the existing sync path fills every BGG-sourced
    field; curated data stays blank, and only the curated `name` is seeded
    from the fetched bgg_name. An id linked to ANY existing game (primary or
    alternate) returns that game instead of duplicating — bgg_id is only
    unique per game, so this lookup is the sole guard. A failed or empty sync
    deletes the just-created game (the collection payload is the sole
    token-free data source, so an id outside the BGG collection has nothing
    to populate from) — no nameless orphans. Never raises.

    `client` (issue #81): an already-logged-in BggClient to reuse — the bulk
    import calls this once per selected item, and N logins for one import
    would be impolite. The caller owns the credential check in that case."""
    result = CreateResult()

    if client is None and (error := bgg_credentials_error(user)):
        result.error = error
        return result

    link = BggLink.objects.filter(bgg_id=bgg_id).select_related("game").first()
    if link is not None:
        result.existing = link.game
        return result

    # Base or expansion? The collection payload never says, and sync_game only
    # chases base links for games already typed as expansions — so probe the
    # anonymous geekitems JSON once, before creation. Best-effort like the
    # sync's own geekitems call: on failure the game defaults to base
    # (curable by hand plus a re-sync).
    game_type = Game.Type.BASE
    # A locally-built client stays anonymous (the probe needs no login) and is
    # NOT handed to sync_game — only a caller-provided client is logged in.
    probe_client = client or BggClient("", "")
    try:
        if parse_geekitem(probe_client.get_geekitem(bgg_id))["expands_bgg_ids"]:
            game_type = Game.Type.EXPANSION
    except (BggError, requests.RequestException):
        pass

    game = Game.objects.create(name="", type=game_type)
    BggLink.objects.create(game=game, bgg_id=bgg_id, is_primary=True)

    sync = sync_game(game, now=now, client=client, user=user)
    if not sync.ok or sync.not_in_collection:
        game.delete()
        collection_username, _, _ = resolve_bgg_credentials(user)
        result.error = sync.error or (
            f"BGG id {bgg_id} is not in the "
            f"{collection_username} BGG collection — "
            "nothing to create it from."
        )
        return result

    game.name = game.bgg_name or f"BGG #{bgg_id}"
    game.save(update_fields=["name"])
    result.game = game
    result.sync = sync
    return result


# --- Bulk collection import (issue #81) --------------------------------------

# BGG API status param -> checkbox label, in form display order — all eight
# collection flags. The import maps whatever combination BGG set on a row to
# ONE action via import_action_from_flags.
IMPORT_STATUS_CHOICES = (
    ("own", "Owned"),
    ("preordered", "Preordered"),
    ("prevowned", "Previously owned"),
    ("fortrade", "For trade"),
    ("wishlist", "Wishlist"),
    ("want", "Want in trade"),
    ("wanttoplay", "Want to play"),
    ("wanttobuy", "Want to buy"),
)
DEFAULT_IMPORT_STATUSES = frozenset({"own", "preordered", "prevowned"})

# What the import does with a candidate, resolved from its merged flags.
IMPORT_ACTION_COPY = "copy"
IMPORT_ACTION_ARCHIVED = "archived_copy"
IMPORT_ACTION_PREORDER = "preorder"
IMPORT_ACTION_WISHLIST = "wishlist"
IMPORT_ACTIONS = {
    IMPORT_ACTION_COPY: "Game + copy",
    IMPORT_ACTION_ARCHIVED: "Game + archived copy",
    IMPORT_ACTION_PREORDER: "Game only (preordered)",
    IMPORT_ACTION_WISHLIST: "Game only (wishlist)",
}


def import_action_from_flags(flags):
    """One import action per candidate, however many flags BGG set on the
    row: own/fortrade -> active Copy (a for-trade copy is still on the
    shelf); else prevowned -> ARCHIVED Copy; else preordered or a want-type
    flag -> Game only. "" when no tracked flag is set (nothing to import).
    NOTE: prevowned outranks preordered HERE, the reverse of the stored-
    status precedence (STATUS_FLAG_PRECEDENCE) — a sold-then-preordered-
    again game should keep its archived copy for history while its stored
    status says preordered. Both orders are deliberate."""
    if flags.get("own") or flags.get("fortrade"):
        return IMPORT_ACTION_COPY
    if flags.get("prevowned"):
        return IMPORT_ACTION_ARCHIVED
    if flags.get("preordered"):
        return IMPORT_ACTION_PREORDER
    if any(flags.get(name) for name in ("wishlist", "want", "wanttoplay", "wanttobuy")):
        return IMPORT_ACTION_WISHLIST
    return ""


@dataclass
class CollectionPreview:
    """Outcome of fetch_collection_candidates — the import view renders this,
    never an exception. candidates/existing are template-ready dicts (bgg_id,
    name, year_published, thumbnail_url, status_labels, wishlist_priority,
    action, action_label; existing rows add "game")."""
    error: str = ""
    candidates: list = field(default_factory=list)
    existing: list = field(default_factory=list)


def fetch_collection_candidates(bgg_username, statuses, *, user=None):
    """Fetch and merge the BGG collection for the preview step. Never raises.

    One login, then one status-filtered collection request per selected
    status (BGG ANDs combined filters, so the union NEEDS a request per
    status). Every payload carries the FULL <status> flag set on each row,
    so the first occurrence of a bgg_id settles its flags and action. Rows
    already linked by ANY BggLink (primary or alternate — the same guard
    create_game_from_bgg uses) land in `existing`, the rest in `candidates`.

    The token-less session realistically only reads settings.BGG_USERNAME's
    own collection (BGG's registration exemption); a different bgg_username
    fails safely here (BGG answers an <errors> payload or an empty/foreign
    collection whose items later sync as not-in-collection and clean up)."""
    preview = CollectionPreview()

    if error := bgg_credentials_error(user):
        preview.error = error
        return preview

    stats = {}
    flags = {}
    client = make_bgg_client(user)
    try:
        client.login()
        for status in statuses:
            xml = client.get_collection(bgg_username, status=status)
            bgg_message = parse_collection_error(xml)
            if bgg_message:
                preview.error = f"BGG: {bgg_message}"
                return preview
            for bgg_id, data in parse_collection(xml).items():
                stats.setdefault(bgg_id, data)
            for bgg_id, item_flags in parse_collection_status_flags(xml).items():
                flags.setdefault(bgg_id, item_flags)
    except BggAuthError as error:
        preview.error = str(error)
        return preview
    except (BggError, requests.RequestException) as error:
        preview.error = f"BGG collection fetch failed: {error}"
        return preview

    linked = {}
    for link in BggLink.objects.filter(bgg_id__in=stats).select_related("game"):
        linked.setdefault(link.bgg_id, link.game)

    for bgg_id in sorted(stats, key=lambda i: (stats[i]["bgg_name"].lower(), i)):
        item_flags = flags.get(bgg_id, {})
        action = import_action_from_flags(item_flags)
        if not action:
            continue  # no tracked flag (malformed row) — nothing to import
        candidate = {
            "bgg_id": bgg_id,
            "name": stats[bgg_id]["bgg_name"],
            "year_published": stats[bgg_id]["year_published"],
            "thumbnail_url": stats[bgg_id]["thumbnail_url"],
            "wishlist_priority": item_flags.get("wishlist_priority"),
            "status_labels": [
                label for param, label in IMPORT_STATUS_CHOICES
                if item_flags.get(param)
            ],
            # Issue #82: carried through to the confirm step so a for-trade
            # row's created copy starts out marked keep_status=WILL_LEAVE.
            "fortrade": bool(item_flags.get("fortrade")),
            "action": action,
            "action_label": IMPORT_ACTIONS[action],
        }
        if bgg_id in linked:
            preview.existing.append({**candidate, "game": linked[bgg_id]})
        else:
            preview.candidates.append(candidate)
    return preview


def group_candidates_by_action(candidates):
    """Bucket preview candidates by resolved action, in IMPORT_ACTIONS'
    display order, for the per-action bulk-select toggle in the preview
    (issue #88). Skips actions with no candidates; keeps each bucket's
    existing (name-sorted) relative order. has_priority flags whether the
    wishlist-priority filter control is worth showing for that group."""
    buckets = {action: [] for action in IMPORT_ACTIONS}
    for candidate in candidates:
        buckets[candidate["action"]].append(candidate)
    return [
        {
            "action": action,
            "label": label,
            "candidates": buckets[action],
            "has_priority": any(c["wishlist_priority"] for c in buckets[action]),
        }
        for action, label in IMPORT_ACTIONS.items()
        if buckets[action]
    ]


@dataclass
class ImportItemResult:
    """One row of an ImportReport: exactly one of game/existing/error is set."""
    bgg_id: int
    action: str
    game: object = None      # created
    existing: object = None  # already linked -> skipped (double-submit safety)
    error: str = ""

    @property
    def action_label(self):
        return IMPORT_ACTIONS.get(self.action, self.action)


@dataclass
class ImportReport:
    """Outcome of import_collection_items — the done page renders this.
    `error` is fatal-only (creds/login failed before the loop ran)."""
    error: str = ""
    items: list = field(default_factory=list)

    @property
    def created(self):
        return [item for item in self.items if item.game is not None]

    @property
    def skipped(self):
        return [item for item in self.items if item.existing is not None]

    @property
    def failed(self):
        return [item for item in self.items if item.error]


def import_collection_items(user, items, *, now=None, fortrade_ids=frozenset()):
    """Import confirmed collection rows: items is [(bgg_id, action), ...].
    fortrade_ids (issue #82) is the subset of those bgg_ids whose BGG row
    carried the fortrade flag — a copy created for one starts out marked
    keep_status=WILL_LEAVE.

    One login, then each id goes through the single-game create path
    (create_game_from_bgg with the shared client — issue #55 per item, as
    #81 specifies), so 202-queue/429 politeness rides the client's backoff.
    Per-item failures are recorded and the loop CONTINUES — earlier items
    stay committed (create_game_from_bgg cleans its own orphans), and a
    re-run skips whatever landed via the BggLink dedup. Copy-type actions
    then get a default Edition + the user's Copy. Never raises."""
    report = ImportReport()

    if error := bgg_credentials_error(user):
        report.error = error
        return report

    client = make_bgg_client(user)
    try:
        client.login()
    except (BggError, requests.RequestException) as error:
        report.error = f"BGG login failed: {error}"
        return report

    for bgg_id, action in items:
        item = ImportItemResult(bgg_id=bgg_id, action=action)
        report.items.append(item)
        result = create_game_from_bgg(bgg_id, now=now, client=client, user=user)
        if result.existing is not None:
            item.existing = result.existing
            continue
        if result.error:
            item.error = result.error
            continue
        item.game = result.game
        if action in (IMPORT_ACTION_COPY, IMPORT_ACTION_ARCHIVED):
            _ensure_import_copy(
                user, result.game, archived=action == IMPORT_ACTION_ARCHIVED,
                will_leave=bgg_id in fortrade_ids,
            )
    return report


def _ensure_import_copy(user, game, *, archived, will_leave=False):
    """Default Edition + the importing user's Copy (import_mastersheet's
    pattern). get_or_create against both unique keys — (game, is_default)
    and (owner, edition) — keeps re-imports idempotent. archive_reason and
    archive_date stay blank: BGG doesn't say why or when a copy left.
    will_leave (issue #82) seeds keep_status=WILL_LEAVE for a BGG for-trade
    row — only applied on creation, like archive_status below, so a re-run
    never overwrites an owner's own later curation edit."""
    edition, _ = Edition.objects.get_or_create(
        game=game, is_default=True, defaults={"name": ""},
    )
    Copy.objects.get_or_create(owner=user, edition=edition, defaults={
        "archive_status": (
            Copy.ArchiveStatus.ARCHIVED if archived else Copy.ArchiveStatus.ACTIVE
        ),
        "keep_status": Copy.KeepStatus.WILL_LEAVE if will_leave else "",
    })


# --- Write-back (issue #117, best-effort/exploratory) ------------------------

# Inverse of STATUS_FLAG_PRECEDENCE: a stored status (or "" = remove from the
# collection entirely) -> the item's new "status" object. Only the chosen
# status's flag is set True; the rest are simply absent, which the live-
# verified REST write-back treats as a full status replacement (issue #157) —
# no need to explicitly zero the others, mirroring this app's own
# single-status precedence (never storing more than one membership status
# per Game at a time).
def _flags_for_status(new_status):
    return {
        Game.BggCollectionStatus.OWN: {"own": True},
        Game.BggCollectionStatus.PREORDERED: {"preordered": True},
        Game.BggCollectionStatus.PREV_OWNED: {"prevowned": True},
        Game.BggCollectionStatus.WISHLIST: {"wishlist": True},
    }.get(new_status, {})


@dataclass
class PushResult:
    """Outcome of push_bgg_status — callers render/log this, never an
    exception."""
    ok: bool = False
    error: str = ""
    no_primary_link: bool = False
    invalid_status: bool = False
    new_status: str = ""
    pushed_at: object = None


def record_push_failure(game, user, note):
    """Upsert a PUSH_FAILED BggSyncDiff for `game` so a write-back failure —
    whether the live BGG POST failed or the Celery enqueue itself failed —
    surfaces on the owner's dashboard like any other sync diff. No-ops
    quietly if the game has no primary BGG link (nothing to key the diff on;
    push_bgg_status already refuses to push in that case)."""
    link = game.primary_bgg_link
    if link is None or user is None:
        return
    now = timezone.now()
    BggSyncDiff.objects.update_or_create(
        owner=user, category=BggSyncDiff.Category.PUSH_FAILED, bgg_id=link.bgg_id,
        defaults={"game": game, "bgg_name": game.bgg_name, "note": note, "last_seen_at": now},
    )


def clear_push_failure(game, user):
    """Delete any open PUSH_FAILED diff for `game` — a push succeeded, or a
    later read confirmed it. Shared with sync_bgg's pending-push reconcile."""
    if user is None:
        return
    link = game.primary_bgg_link
    if link is None:
        return
    BggSyncDiff.objects.filter(
        owner=user, category=BggSyncDiff.Category.PUSH_FAILED, bgg_id=link.bgg_id,
    ).delete()


def accept_prev_owned_active(user, game):
    """Issue #168 accept action for a PREV_OWNED_ACTIVE diff: BGG says the
    game already left the collection, so archive every active,
    non-borrowed-in Copy the user has for it. archive_reason/archive_date
    stay blank — same rationale as _ensure_import_copy: BGG doesn't say why
    or when."""
    copies = Copy.objects.filter(
        owner=user, edition__game=game,
        archive_status=Copy.ArchiveStatus.ACTIVE, is_borrowed_in=False,
    )
    for copy in copies:
        copy.archive_status = Copy.ArchiveStatus.ARCHIVED
        copy.archive_reason = ""
        copy.archive_date = None
        copy.save(update_fields=[
            "archive_status", "archive_reason", "archive_date", "updated_at",
        ])
    clear_push_failure(game, user)


def accept_archived_on_bgg(user, game):
    """Issue #168 accept action for an ARCHIVED_ON_BGG diff: BGG still says
    the game is owned, so reactivate the most-recently-archived Copy the
    user has for it (ties broken by pk). archive_reason/archive_date are
    cleared — the copy is active again, so they no longer apply. No-ops the
    Copy mutation if the diff has gone stale and no archived copy remains,
    but still clears a stale push-failure."""
    copy = Copy.objects.filter(
        owner=user, edition__game=game, archive_status=Copy.ArchiveStatus.ARCHIVED,
    ).order_by(F("archive_date").desc(nulls_last=True), "-pk").first()
    if copy is not None:
        copy.archive_status = Copy.ArchiveStatus.ACTIVE
        copy.archive_reason = ""
        copy.archive_date = None
        copy.save(update_fields=[
            "archive_status", "archive_reason", "archive_date", "updated_at",
        ])
    clear_push_failure(game, user)


def push_is_pending(game, now):
    """True while a push_bgg_status write is still within PUSH_CONFIRM_WINDOW
    and hasn't been confirmed (or expired) by a later read sync."""
    return (
        bool(game.bgg_status_pushed_at)
        and now - game.bgg_status_pushed_at < PUSH_CONFIRM_WINDOW
    )


def push_bgg_status(game, new_status, *, priority=None, now=None, client=None, user=None):
    """Push one Game's collection status up to BGG (issue #117), via the
    real REST write-back API confirmed live for issue #157: read the game's
    current collection item, replace its "status" object, PUT the full item
    back. Never raises — the outcome comes back on the returned PushResult.

    Adding a BRAND-NEW collection item (a game with no existing BGG entry at
    all) is deliberately unsupported — the write shape for that path hasn't
    been verified live (issue #157's still-open question), so this refuses
    with a clear error rather than guessing.

    Mirrors sync_game's shape: network phase outside the transaction, then a
    small write phase inside one. On success, writes bgg_collection_status
    (+ wishlist priority) plus the bgg_status_pushed/_at marker via a plain
    save() — deliberately NOT apply_bgg_fields, since a push must never stamp
    last_synced_at (that timestamp means "last READ sync"). On failure the
    local status is left untouched (keeps the app consistent with BGG's
    actual, unchanged state) and a PUSH_FAILED diff is recorded instead."""
    result = PushResult(new_status=new_status)

    if new_status not in Game.BggCollectionStatus.values and new_status != "":
        result.invalid_status = True
        return result

    if client is None and (error := bgg_credentials_error(user)):
        result.error = error
        return result

    link = game.primary_bgg_link
    if link is None:
        result.no_primary_link = True
        return result
    bgg_id = link.bgg_id
    now = now or timezone.now()

    needs_login = client is None
    if needs_login:
        client = make_bgg_client(user)
    try:
        if needs_login:
            client.login()
        userid = client.get_user_id()
        item = client.get_collection_item(bgg_id, userid)
        if item is None:
            if new_status == "":
                pass  # already absent from BGG — nothing to clear
            else:
                result.error = (
                    "This game isn't in the BGG collection yet — pushing a "
                    "brand-new collection item isn't supported yet (issue #157)."
                )
                record_push_failure(game, user, result.error)
                return result
        else:
            item["status"] = _flags_for_status(new_status)
            if new_status == Game.BggCollectionStatus.WISHLIST:
                item["wishlistpriority"] = priority
            client.put_collection_item(item)
    except BggAuthError as error:
        result.error = str(error)
        record_push_failure(game, user, f"Push failed: {error}")
        return result
    except (BggError, requests.RequestException) as error:
        result.error = f"BGG push failed: {error}"
        record_push_failure(game, user, result.error)
        return result

    with transaction.atomic():
        game.bgg_collection_status = new_status
        game.bgg_wishlist_priority = (
            priority if new_status == Game.BggCollectionStatus.WISHLIST else None
        )
        game.bgg_status_pushed = new_status
        game.bgg_status_pushed_at = now
        game.save(update_fields=[
            "bgg_collection_status", "bgg_wishlist_priority",
            "bgg_status_pushed", "bgg_status_pushed_at",
        ])
        clear_push_failure(game, user)

    result.ok = True
    result.pushed_at = now
    return result


@dataclass
class FortradePushResult:
    """Outcome of push_bgg_fortrade — callers render/log this, never an
    exception."""
    ok: bool = False
    error: str = ""
    no_primary_link: bool = False
    fortrade: bool = False
    pushed_at: object = None


def push_bgg_fortrade(game, fortrade, *, now=None, client=None, user=None):
    """Push `game`'s "for trade" flag up to BGG (issue #82). Unlike
    push_bgg_status, BGG's fortrade flag is orthogonal to the single
    membership status (a for-trade copy is still "own"), so this MERGES the
    flag into the collection item's existing status object instead of
    replacing it — the one deliberate structural difference from
    push_bgg_status's _flags_for_status replacement. Mirrors
    push_bgg_status otherwise: never raises, network phase outside the
    transaction, a small write phase inside one, failures recorded via
    record_push_failure/clear_push_failure, and the write uses a plain
    save() (never apply_bgg_fields) so last_synced_at is untouched."""
    result = FortradePushResult(fortrade=fortrade)

    if client is None and (error := bgg_credentials_error(user)):
        result.error = error
        return result

    link = game.primary_bgg_link
    if link is None:
        result.no_primary_link = True
        return result
    bgg_id = link.bgg_id
    now = now or timezone.now()

    needs_login = client is None
    if needs_login:
        client = make_bgg_client(user)
    try:
        if needs_login:
            client.login()
        userid = client.get_user_id()
        item = client.get_collection_item(bgg_id, userid)
        if item is None:
            if not fortrade:
                pass  # already absent from BGG — nothing to clear
            else:
                result.error = (
                    "This game isn't in the BGG collection yet — pushing a "
                    "brand-new collection item isn't supported yet (issue #157)."
                )
                record_push_failure(game, user, result.error)
                return result
        else:
            if fortrade:
                item["status"]["fortrade"] = True
            else:
                item["status"].pop("fortrade", None)
            client.put_collection_item(item)
    except BggAuthError as error:
        result.error = str(error)
        record_push_failure(game, user, f"Push failed: {error}")
        return result
    except (BggError, requests.RequestException) as error:
        result.error = f"BGG push failed: {error}"
        record_push_failure(game, user, result.error)
        return result

    with transaction.atomic():
        game.bgg_fortrade_pushed = fortrade
        game.bgg_fortrade_pushed_at = now
        game.save(update_fields=["bgg_fortrade_pushed", "bgg_fortrade_pushed_at"])
        clear_push_failure(game, user)

    result.ok = True
    result.pushed_at = now
    return result
