"""BGG XML API2 client and parsers (DESIGN §8).

BGG (2025) put the XML API behind auth. There are two ways in, and login()
is the seam between them (DESIGN §8 phase 1 vs phase 2):

- Registered-app Bearer token (preferred): pass a token and login() sets an
  Authorization: Bearer <token> header — no POST. This unlocks the
  token-gated /thing (weight + stats for games outside the collection).
- Token-less fallback: BGG's registration exceptions exempt downloading YOUR
  OWN collection from app registration, so a plain logged-in session works.
  The client POSTs to /login/api/v1 ({"credentials": {"username": ...,
  "password": ...}} -> session cookies) and then talks to xmlapi2 with those
  cookies. /thing 401s in this mode, so weight stays unreachable.

A self-hoster who doesn't register an app keeps syncing token-free (own
collection only); a token, when configured, is the only thing that changes.

Verified live 2026-07-03 (§15 open question CLOSED): /collection for the
logged-in user's OWN account returns 200 with the session, but /thing
returns 401 "Unauthorized" IDENTICALLY with or without the session cookies
— the exemption is scoped to your own collection, and /thing is gated by
app registration, not login. So WEIGHT (only on /thing) and stats for
games outside the collection are unreachable token-less; they wait for the
Bearer token. The collection payload is the whole DOCUMENTED token-free
surface: it carries name/year/images/players/playtime/rank/rating plus
<numplays> and the wishlist/wanttoplay status flags — but never
averageweight. One undocumented escape hatch (also probed 2026-07-03):
/api/geekitems — the JSON endpoint BGG's own frontend calls — answers 200
even anonymously and carries the expansion->base links; see get_geekitem
for the caveats. The login endpoint answers 400 +
{"errors": {"message": ...}} for bad credentials.

Polite by design (§8): serial requests only, retry-with-backoff on the
collection queue's 202 and on 429/5xx — an unregistered session has no
rate-limit allowance to burn.

The parsers are pure text -> dict functions keyed by the Game model fields
they fill; matching and writing live in the sync_bgg command. This module
is kept command-free so the weekly Celery beat task (§8) can reuse it.

Write-back (issue #117, verified live 2026-07-15 — issue #157): xmlapi2 has
no collection-write operation, and the originally reverse-engineered
`geekcollection.php` POST turned out to be dead for this purpose (Cloudflare
403s a plain session, and its guessed `action` verbs misbehave even from a
real browser). BGG's own current frontend instead calls a modern REST API
that works fine over the same logged-in session cookies `login()` sets up:
`get_user_id` resolves the session's own numeric userid, `get_collection_item`
reads one item as JSON (its `status` sub-object lists only the TRUE flags —
verified sparse, not an all-flags dict like the XML API), and
`put_collection_item` PUTs the FULL item back with `status` replaced
wholesale (not a minimal diff — verified live, including a real
own->prevowned->own status transition). See those methods' docstrings.
"""

import json
import re
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation
from xml.etree import ElementTree

import requests

BGG_ID_RE = re.compile(r"boardgamegeek\.com/\w+/(\d+)")


def extract_bgg_id(url):
    """Return the numeric BGG thing id from a boardgamegeek.com URL, or None."""
    if not url:
        return None
    match = BGG_ID_RE.search(url)
    return int(match.group(1)) if match else None


class BggError(Exception):
    """Any BGG API failure the caller may want to handle."""


class BggAuthError(BggError):
    """Login rejected, or an endpoint refused the session (401)."""


class BggClient:
    LOGIN_URL = "https://boardgamegeek.com/login/api/v1"
    COLLECTION_URL = "https://boardgamegeek.com/xmlapi2/collection"
    THING_URL = "https://boardgamegeek.com/xmlapi2/thing"
    PLAYS_URL = "https://boardgamegeek.com/xmlapi2/plays"
    GEEKITEMS_URL = "https://boardgamegeek.com/api/geekitems"
    USER_CURRENT_URL = "https://boardgamegeek.com/api/users/current"
    COLLECTIONS_API_URL = "https://boardgamegeek.com/api/collections"
    COLLECTION_ITEM_URL = "https://boardgamegeek.com/api/collectionitem/{}"
    USER_AGENT = "GameKeeper/0.1 (personal collection tracker)"
    # 202-queued / 429 / 5xx retry delays (§8 retry-with-backoff).
    BACKOFF_DELAYS = (2, 5, 10, 20, 40, 60)
    RETRY_STATUSES = frozenset({202, 429, 500, 502, 503, 504})

    def __init__(self, username, password, token=""):
        self.username = username
        self.password = password
        self.token = token
        self.session = requests.Session()
        self.session.headers["User-Agent"] = self.USER_AGENT

    def login(self):
        """Authenticate the session (§8). With a registered-app token, set the
        Bearer header and skip the POST — this is the only path that unlocks
        /thing. Otherwise POST username/password for own-collection access."""
        if self.token:
            self.session.headers["Authorization"] = f"Bearer {self.token}"
            return
        response = self.session.post(
            self.LOGIN_URL,
            json={"credentials": {"username": self.username, "password": self.password}},
            timeout=30,
        )
        if response.status_code == 400:
            try:
                message = response.json()["errors"]["message"]
            except (ValueError, KeyError):
                message = "login rejected"
            raise BggAuthError(f"BGG login failed: {message}")
        response.raise_for_status()

    def get_collection(self, bgg_username, status="own", bgg_id=None):
        """Collection XML with stats. status filters to ONE membership status
        (own / preordered / prevowned) — BGG ANDs combined status filters, so a
        caller wanting the union issues one request per status. Pass
        status=None to omit the filter entirely: the item then carries EVERY
        status flag (own/preordered/prevowned/wishlist) on its <status>
        element, so one request settles a game's membership (used by the
        per-game refresh, issue #44). bgg_id restricts the payload to specific
        thing id(s) via BGG's id= filter — one game instead of the whole
        collection. BGG is the source of truth for membership (§8)."""
        params = {"username": bgg_username, "stats": 1}
        if status is not None:
            params[status] = 1
        if bgg_id is not None:
            params["id"] = bgg_id
        response = self._get_with_backoff(self.COLLECTION_URL, params)
        if response.status_code == 401:
            raise BggAuthError("BGG refused the session on /collection (401).")
        response.raise_for_status()
        return response.text

    def get_things(self, bgg_ids):
        """One thing request for a batch of ids. Raises BggAuthError on 401 so
        the caller can degrade — whether /thing accepts a mere logged-in
        session is the DESIGN §15 open question, answered at runtime."""
        response = self._get_with_backoff(
            self.THING_URL,
            {"id": ",".join(str(i) for i in bgg_ids), "stats": 1},
        )
        if response.status_code == 401:
            raise BggAuthError("BGG refused the session on /thing (401).")
        response.raise_for_status()
        return response.text

    def get_geekitem(self, bgg_id):
        """UNDOCUMENTED JSON endpoint (BGG's own frontend uses it). Probed
        live 2026-07-03: answers 200 with NO auth at all and carries the
        expansion->base links (links.expandsboardgame) that the 401-gated
        /thing withholds. One item per request — no batching — and BGG
        could change or gate it without notice, so callers keep it out of
        the scheduled sync and stay polite (serial, paused, low volume)."""
        response = self._get_with_backoff(
            self.GEEKITEMS_URL, {"objectid": bgg_id, "objecttype": "thing"},
        )
        if response.status_code == 401:
            raise BggAuthError("BGG refused the request on /api/geekitems (401).")
        response.raise_for_status()
        return response.text

    def get_user_id(self):
        """The logged-in session's own numeric BGG userid (issue #157),
        needed by the real write-back API's ?userid= filter. Resolved
        straight off the session cookies via BGG's own "who am I" endpoint —
        xmlapi2/user (username -> id) turned out to be token-gated exactly
        like /thing, so it can't be used by a token-less session. Raises
        BggAuthError on 401 like the other authed calls."""
        response = self._get_with_backoff(self.USER_CURRENT_URL, {})
        if response.status_code == 401:
            raise BggAuthError("BGG refused the session on api/users/current (401).")
        response.raise_for_status()
        return response.json()["userid"]

    def get_collection_item(self, bgg_id, userid):
        """The one collection item BGG's own frontend reads for its
        collection-status widget (issue #157) — the verified write-back read
        side, replacing the dead `geekcollection.php` guess. Returns the
        full item dict (carries `collid`, a `status` sub-object listing only
        the flags that are TRUE — verified live, not an all-flags dict like
        the XML API — plus `wishlistpriority` and unrelated fields like
        pricepaid/comment that put_collection_item must round-trip
        unchanged), or None if bgg_id isn't in userid's collection at all."""
        response = self._get_with_backoff(
            self.COLLECTIONS_API_URL,
            {"objectid": bgg_id, "objecttype": "thing", "userid": userid},
        )
        if response.status_code == 401:
            raise BggAuthError("BGG refused the session on api/collections (401).")
        response.raise_for_status()
        items = response.json().get("items") or []
        return items[0] if items else None

    def put_collection_item(self, item):
        """Write `item` back (issue #157). BGG's own frontend always PUTs
        the FULL item object back, never a minimal diff — verified live,
        including a real own -> prevowned -> own status round-trip — so
        callers mutate a dict returned by get_collection_item (typically
        just its "status" key) and pass the whole thing back here. Keyed by
        item["collid"]. Raises BggAuthError on 401 like the read endpoints.
        Never called by the scheduled read sync — only by
        bgg_sync.push_bgg_status."""
        response = self._put_with_backoff(
            self.COLLECTION_ITEM_URL.format(item["collid"]), {"item": item},
        )
        if response.status_code == 401:
            raise BggAuthError("BGG refused the session on api/collectionitem (401).")
        response.raise_for_status()
        return response.json()

    def get_plays(self, bgg_username, *, page=1, bgg_id=None):
        """One page of a user's play history (xmlapi2/plays — DESIGN §8 plays
        log). BGG paginates at 100 plays/page, so the caller loops on the
        <plays total> count (see parse_plays). bgg_id restricts the history to
        one thing (the per-game refresh). Raises BggAuthError on 401 — private
        plays need the Bearer token, so a token-less session degrades instead of
        failing the whole sync (like get_things)."""
        params = {"username": bgg_username, "page": page}
        if bgg_id is not None:
            params["id"] = bgg_id
        response = self._get_with_backoff(self.PLAYS_URL, params)
        if response.status_code == 401:
            raise BggAuthError("BGG refused the session on /plays (401).")
        response.raise_for_status()
        return response.text

    def _get_with_backoff(self, url, params):
        response = self.session.get(url, params=params, timeout=60)
        for delay in self.BACKOFF_DELAYS:
            if response.status_code not in self.RETRY_STATUSES:
                return response
            time.sleep(delay)
            response = self.session.get(url, params=params, timeout=60)
        if response.status_code in self.RETRY_STATUSES:
            raise BggError(
                f"BGG kept answering {response.status_code} after "
                f"{len(self.BACKOFF_DELAYS) + 1} attempts ({url})."
            )
        return response

    def _put_with_backoff(self, url, json_body):
        response = self.session.put(url, json=json_body, timeout=60)
        for delay in self.BACKOFF_DELAYS:
            if response.status_code not in self.RETRY_STATUSES:
                return response
            time.sleep(delay)
            response = self.session.put(url, json=json_body, timeout=60)
        if response.status_code in self.RETRY_STATUSES:
            raise BggError(
                f"BGG kept answering {response.status_code} after "
                f"{len(self.BACKOFF_DELAYS) + 1} attempts ({url})."
            )
        return response


# --- Parsers -----------------------------------------------------------------
# Both return {bgg_id: {game_field: value}}. Values follow the Game model's
# conventions: "" for blank text fields, None for unknown numerics. BGG uses
# 0 / "N/A" / "Not Ranked" as its "unknown" markers — all become None.
# One non-field key: parse_things' "expands_bgg_ids" (base-game BGG ids for
# the Game.expands M2M — resolved to Games by the sync command, issue #40).


def parse_collection(xml_text):
    """collection?stats=1 items. Carries name/year/images/players/playtime/
    rank/rating and the owner's <numplays> — but NOT weight (that only
    exists on the token-gated /thing)."""
    items = {}
    for element in ElementTree.fromstring(xml_text).iter("item"):
        bgg_id = int(element.get("objectid"))
        if bgg_id in items:
            # A second owned copy of the same thing — identical stats.
            continue
        data = {
            "bgg_name": (element.findtext("name") or "").strip(),
            "year_published": _to_int(element.findtext("yearpublished")),
            "image_url": (element.findtext("image") or "").strip(),
            "thumbnail_url": (element.findtext("thumbnail") or "").strip(),
            # <numplays> is token-free (collection-only); 0 plays -> None
            # (unknown), so the UI only shows a positive count.
            "bgg_numplays": _to_int(element.findtext("numplays")),
        }
        stats = element.find("stats")
        if stats is not None:
            data["min_players"] = _to_int(stats.get("minplayers"))
            data["max_players"] = _to_int(stats.get("maxplayers"))
            data["min_playtime"] = _to_int(stats.get("minplaytime"))
            data["max_playtime"] = _to_int(stats.get("maxplaytime"))
            rating = stats.find("rating")
            if rating is not None:
                data["bgg_rating"] = _to_decimal(_attr_value(rating, "average"), "0.001")
                data["bgg_rank"] = _boardgame_rank(rating.find("ranks"))
        items[bgg_id] = data
    return items


def parse_collection_status_flags(xml_text):
    """{bgg_id: {flag: bool, "wishlist_priority": int|None}} from each item's
    <status> element — all eight BGG membership booleans (issue #81 extended
    the original own/preordered/prevowned/wishlist four) plus the wishlist
    priority (1 must-have … 5 don't-buy; None when not wishlisted). Model-free
    (the caller maps the flags to a stored status with its own precedence). A
    collection fetched WITHOUT a status filter carries every flag on the item,
    so this settles a game's membership from a single request (issue #44)."""
    flags = {}
    for element in ElementTree.fromstring(xml_text).iter("item"):
        status = element.find("status")
        if status is None:
            continue
        item = {
            name: status.get(name) == "1"
            for name in (
                "own", "preordered", "prevowned", "fortrade",
                "want", "wanttoplay", "wanttobuy", "wishlist",
            )
        }
        item["wishlist_priority"] = _to_int(status.get("wishlistpriority"))
        flags[int(element.get("objectid"))] = item
    return flags


def parse_collection_error(xml_text):
    """BGG answers HTTP 200 with an <errors> document (e.g. "Invalid username
    specified") instead of a collection — return its first <message> text, or
    "" for a normal payload. Without this check a bad username masquerades as
    an empty collection."""
    root = ElementTree.fromstring(xml_text)
    if root.tag != "errors":
        return ""
    return (root.findtext(".//message") or "BGG returned an error.").strip()


def parse_things(xml_text):
    """thing?stats=1 items: everything the collection carries plus weight
    (averageweight), the expansion->base links, and mechanic tags
    (DESIGN §10's Tag(kind=mechanic) source).

    "expands_bgg_ids" (issue #40) lists the BASE games this item expands:
    on an expansion's thing item, the boardgameexpansion links pointing at
    its base game(s) carry inbound="true". The same link type WITHOUT
    inbound appears on a base game's item pointing at its expansions —
    that direction is ignored (every primary id in the DB gets its own
    thing item, so the inbound side covers all pairs).

    "mechanics" is the sorted, deduped list of boardgamemechanic link
    values (names) — BGG has no stable id on the Tag model to key by, so
    name is the join key, same as every other Tag."""
    items = {}
    for element in ElementTree.fromstring(xml_text).iter("item"):
        bgg_id = int(element.get("id"))
        name = ""
        for name_element in element.iter("name"):
            if name_element.get("type") == "primary":
                name = (name_element.get("value") or "").strip()
                break
        data = {
            "expands_bgg_ids": [
                int(link.get("id"))
                for link in element.iter("link")
                if link.get("type") == "boardgameexpansion"
                and link.get("inbound") == "true"
            ],
            "mechanics": sorted({
                (link.get("value") or "").strip()
                for link in element.iter("link")
                if link.get("type") == "boardgamemechanic"
                and (link.get("value") or "").strip()
            }),
            "bgg_name": name,
            "year_published": _to_int(_attr_value(element, "yearpublished")),
            "image_url": (element.findtext("image") or "").strip(),
            "thumbnail_url": (element.findtext("thumbnail") or "").strip(),
            "min_players": _to_int(_attr_value(element, "minplayers")),
            "max_players": _to_int(_attr_value(element, "maxplayers")),
            "min_playtime": _to_int(_attr_value(element, "minplaytime")),
            "max_playtime": _to_int(_attr_value(element, "maxplaytime")),
        }
        ratings = element.find("statistics/ratings")
        if ratings is not None:
            data["bgg_rating"] = _to_decimal(_attr_value(ratings, "average"), "0.001")
            data["weight"] = _to_decimal(_attr_value(ratings, "averageweight"), "0.01")
            data["bgg_rank"] = _boardgame_rank(ratings.find("ranks"))
        items[bgg_id] = data
    return items


def parse_plays(xml_text):
    """xmlapi2/plays -> (plays, total). Read-only play history (DESIGN §8): the
    plays this account posted back to BGG (BG Stats auto-post), NOT the app's own
    log — GameKeeper never writes plays. Model-free, like the other parsers;
    the sync joins each play's `objectid` to a primary BggLink and upserts.

    `total` is the <plays total> attribute (all pages), so a caller pages until
    it has collected that many (BGG serves 100/page). Each play dict:
    external_id (the stable <play id>), objectid, play_date (None if BGG gives a
    blank/zero date), quantity, length_minutes (None when 0/unknown), location,
    incomplete, comments, and players=[{name, username, score, won, is_new,
    color, start_position, rating}] — scores/ratings stay text ("" when blank),
    since BGG allows non-numeric scores."""
    root = ElementTree.fromstring(xml_text)
    total = _to_int(root.get("total")) or 0
    plays = []
    for element in root.iter("play"):
        item = element.find("item")
        players = [
            {
                "name": (player.get("name") or "").strip(),
                "username": (player.get("username") or "").strip(),
                "score": (player.get("score") or "").strip(),
                "won": player.get("win") == "1",
                "is_new": player.get("new") == "1",
                "color": (player.get("color") or "").strip(),
                "start_position": (player.get("startposition") or "").strip(),
                "rating": (player.get("rating") or "").strip(),
            }
            for player in element.iter("player")
        ]
        plays.append({
            "external_id": (element.get("id") or "").strip(),
            "objectid": _to_int(item.get("objectid")) if item is not None else None,
            "play_date": _to_date(element.get("date")),
            # A play is at least one play; BGG's quantity is >=1 (0 -> 1).
            "quantity": _to_int(element.get("quantity")) or 1,
            "length_minutes": _to_int(element.get("length")),
            "location": (element.get("location") or "").strip(),
            "incomplete": element.get("incomplete") == "1",
            "comments": (element.findtext("comments") or "").strip(),
            "players": players,
        })
    return plays, total


def parse_plays_error(xml_text):
    """Like parse_collection_error: BGG answers 200 with an <errors> document
    (e.g. an invalid username) instead of a <plays> feed — return its first
    message, or "" for a normal payload."""
    root = ElementTree.fromstring(xml_text)
    if root.tag != "errors":
        return ""
    return (root.findtext(".//message") or "BGG returned an error.").strip()


def parse_geekitem(json_text):
    """geekitems JSON for ONE thing -> the same "expands_bgg_ids" key
    parse_things emits (base-game ids from links.expandsboardgame), so the
    two sources feed identical linking code. Also carries "expansions"
    (issue #64): a base game's OWN links.boardgameexpansion list — the
    outbound direction parse_things ignores (see its docstring) — as
    {"bgg_id", "name"} dicts, used by sync_new_expansions to discover
    expansions that aren't in the app yet. Extend here if more geekitems
    data (mechanics for §10?) ever gets used."""
    item = json.loads(json_text).get("item") or {}
    links = item.get("links") or {}
    return {
        "expands_bgg_ids": [
            int(entry["objectid"])
            for entry in links.get("expandsboardgame") or []
        ],
        "expansions": [
            {"bgg_id": int(entry["objectid"]), "name": (entry.get("name") or "").strip()}
            for entry in links.get("boardgameexpansion") or []
        ],
    }


def _attr_value(parent, tag):
    """The value="..." attribute of a child element, or None."""
    element = parent.find(tag)
    return element.get("value") if element is not None else None


def _boardgame_rank(ranks_element):
    """The overall Board Game Rank (name="boardgame"); "Not Ranked" -> None."""
    if ranks_element is None:
        return None
    for rank in ranks_element.iter("rank"):
        if rank.get("name") == "boardgame":
            return _to_int(rank.get("value"))
    return None


def _to_date(raw):
    """BGG play date 'YYYY-MM-DD' -> date, or None for blank / '0000-00-00' /
    otherwise unparseable (some legacy plays carry a partial date)."""
    try:
        return datetime.strptime((raw or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def _to_int(raw):
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value or None


def _to_decimal(raw, quant):
    """Decimal quantized to the model field's precision, so a re-sync compares
    equal to the stored value instead of registering a phantom change."""
    try:
        value = Decimal(str(raw).strip()).quantize(Decimal(quant))
    except (TypeError, ValueError, InvalidOperation):
        return None
    return value if value else None
