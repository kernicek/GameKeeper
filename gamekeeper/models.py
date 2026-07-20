"""Core data model for GameKeeper.

Implements the DESIGN.md §4 spine (Game -> Edition -> Copy) plus the §3
group/membership/visibility boundary, the §9 Location that a Copy points at,
the §5 sleeves module (CardSize / SleeveProduct / requirements / inventory /
per-copy status), the §6 purchases module (Purchase -> Wave -> Product) and
the §10 taxonomy (Tag M2M + curated per-game fields + DigitalImplementation).

Deferred modules (documents §7) are intentionally NOT modelled here. Where a
seam is needed so those can bolt on later without a rewrite, it is called out
in a comment.
"""

import hashlib
import math
import os
import re
import secrets
from collections import defaultdict
from decimal import Decimal
from pathlib import PurePosixPath

from django.conf import settings
from django.contrib.contenttypes.fields import (
    GenericForeignKey,
    GenericRelation,
)
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage
from django.db import models
from simple_history.models import HistoricalRecords

from .cover_preview import render_square_preview


# --- External URL templates -------------------------------------------------
# DESIGN §4: a link *type* optionally has a URL template. If present we store an
# id and derive the URL in one place; if not, we store the full pasted URL.

BGG_THING_URL_TEMPLATE = "https://boardgamegeek.com/boardgame/{id}"


# ===========================================================================
# §3  Groups, membership & visibility
# ===========================================================================

class Group(models.Model):
    """The universal collection / sharing boundary (DESIGN §3).

    Every user belongs to exactly one Group (a "group of one" auto-created on
    signup; households join later). A Group unions its members' Copies into one
    shared collection view.
    """

    class Visibility(models.TextChoices):
        PRIVATE = "private", "Private (group members only)"
        SHARED = "shared", "Shared (explicit grants)"
        SERVER_PUBLIC = "server_public", "Server-public (any logged-in user)"
        # Tier 4 (anonymous share link) is enabled by populating share_token.

    name = models.CharField(max_length=200)
    slug = models.SlugField(max_length=220, unique=True)
    visibility = models.CharField(
        max_length=20, choices=Visibility.choices, default=Visibility.PRIVATE,
    )
    # DESIGN §3 tier 4: unguessable token for the anonymous, curated projection.
    # Null = no anonymous link. ShareGrant (tier 2 targeting) is deferred.
    share_token = models.CharField(
        max_length=64, unique=True, null=True, blank=True,
        help_text="Unguessable token for the anonymous read-only share link.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name

    def enable_share_link(self):
        """Mint the tier-4 anonymous share token (DESIGN §3) if absent.

        token_urlsafe(32) gives 256 bits — unguessable, and short enough for
        the 64-char column. Revoking = blanking share_token (admin action).
        """
        if not self.share_token:
            self.share_token = secrets.token_urlsafe(32)
            self.save(update_fields=["share_token"])
        return self.share_token

    def is_viewable_by(self, user):
        """DESIGN §3 tiers 1-3 for the logged-in /g/<slug>/ views (tier 4 has
        its own token gate). Members always see their own group; otherwise the
        visibility tier decides — the tiers are modal: server_public admits any
        authenticated user, shared admits explicit ShareGrant targets only, and
        private admits nobody (grants left behind on a private group are inert,
        not a leak).
        """
        if not user.is_authenticated:
            return False
        membership = getattr(user, "membership", None)
        if membership is not None and membership.group_id == self.pk:
            return True
        if self.visibility == self.Visibility.SERVER_PUBLIC:
            return True
        if self.visibility == self.Visibility.SHARED:
            targets_viewer = models.Q(grantee_user=user)
            if membership is not None:
                targets_viewer |= models.Q(grantee_group_id=membership.group_id)
            return self.share_grants.filter(targets_viewer).exists()
        return False


class Membership(models.Model):
    """A user's place in a Group, with a role (DESIGN §3 roles).

    v1 constraint — "every user belongs to exactly one Group" — is enforced by
    the unique user FK. Dropping unique=True is the single change needed to
    allow the deferred multi-group membership (DESIGN §14).
    """

    class Role(models.TextChoices):
        OWNER = "owner", "Owner / admin"
        MEMBER = "member", "Member"
        # "Viewer" (external read-only) is granted via visibility/ShareGrant,
        # not by a Membership, so it is not a Role here.

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="membership",
    )
    group = models.ForeignKey(
        Group, on_delete=models.CASCADE, related_name="memberships",
    )
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.MEMBER)
    joined_at = models.DateTimeField(auto_now_add=True)
    # BGG account whose collection feeds the bulk import (issue #81). Rides
    # Membership until the Membership -> Profile rename (separate issue) —
    # this is per-user identity, not group membership.
    bgg_username = models.CharField(max_length=100, blank=True)
    # BGG account password, Fernet-encrypted at rest (issue #118). Holds the
    # ciphertext token string, "" when unset. Write-only from the UI: never
    # rendered back. Auth precedence lives in bgg_sync.resolve_bgg_credentials.
    bgg_password_encrypted = models.TextField(blank=True, default="")
    # ntfy topic this user's reminder pushes go to (issue #162). Per-user, not
    # a secret — blank means the user hasn't opted into pushes, so
    # send_reminder_emails skips them (email-only, as before).
    ntfy_topic = models.CharField(max_length=100, blank=True, default="")

    def __str__(self):
        return f"{self.user} in {self.group} ({self.role})"

    def set_bgg_password(self, raw_password):
        """Encrypt and store the BGG password (or clear it for a falsy value).
        Caller is responsible for saving the instance."""
        from gamekeeper.crypto import encrypt
        self.bgg_password_encrypted = encrypt(raw_password or "")

    def get_bgg_password(self):
        """The decrypted BGG password, or "" when unset/undecryptable."""
        from gamekeeper.crypto import decrypt
        return decrypt(self.bgg_password_encrypted)

    @property
    def has_bgg_password(self):
        """Whether a stored password exists — the write-only 'is set?' signal."""
        return bool(self.bgg_password_encrypted)


class ShareGrant(models.Model):
    """A targeted read-only grant of one group's collection (DESIGN §3 tier 2).

    Exactly one of grantee_user / grantee_group is set. Grants only take
    effect while the granting group's visibility is SHARED (see
    Group.is_viewable_by) — they express *who* the "Shared" tier means, they
    do not override the tier itself. Managed via the admin in v1; revoking =
    deleting the row.
    """

    group = models.ForeignKey(
        Group, on_delete=models.CASCADE, related_name="share_grants",
    )
    grantee_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        null=True, blank=True, related_name="share_grants_received",
    )
    grantee_group = models.ForeignKey(
        Group, on_delete=models.CASCADE,
        null=True, blank=True, related_name="share_grants_received",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(grantee_user__isnull=False,
                             grantee_group__isnull=True)
                    | models.Q(grantee_user__isnull=True,
                               grantee_group__isnull=False)
                ),
                name="sharegrant_exactly_one_grantee",
            ),
            models.UniqueConstraint(
                fields=["group", "grantee_user"],
                name="sharegrant_unique_per_user",
            ),
            models.UniqueConstraint(
                fields=["group", "grantee_group"],
                name="sharegrant_unique_per_group",
            ),
        ]

    def __str__(self):
        return f"{self.group} shared with {self.grantee_user or self.grantee_group}"


class Invite(models.Model):
    """A pending invitation for an existing user to join a Group as a
    Member (DESIGN §3). Accepting re-points the invitee's Membership and
    deletes their old "group of one" — the owner-facing counterpart to
    ShareGrant, but for membership instead of a read-only view."""

    group = models.ForeignKey(Group, on_delete=models.CASCADE, related_name="invites")
    invited_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="invites_received",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["group", "invited_user"], name="invite_unique_per_user",
            ),
        ]

    def __str__(self):
        return f"{self.invited_user} invited to {self.group}"


# ===========================================================================
# §4  Game  (a BGG "thing" / a title)
# ===========================================================================

# Issue #6: name sort ignores a leading English article (Czech has none, so
# English suffices).
_LEADING_ARTICLE = re.compile(r"^(the|a|an)\s+", re.IGNORECASE)

# Issue #98: separators BGG puts between a base-game prefix and the expansion
# tail. ": " is the norm; " - " (and the dash variants) show up when the base
# title itself contains a colon (e.g. "Sleeping Gods - Distant Skies").
_EXPANSION_SEPARATORS = (": ", " - ", " – ", " — ")


class CoverArtModel(models.Model):
    """Abstract cover-art contract (DESIGN §13, issue #54): the local cover
    file plus the focus/zoom/fit machinery the square grid tiles and the
    cover editor operate on. Inherited by Game and Series (and any future
    cover-bearing entity) so both get identical columns and behavior;
    subclasses override cover_url for their own fallback chain."""

    # Local cover file (hand-replaced or fetched — the cover editor at
    # /<entity>/<pk>/cover/ accepts an upload or an image URL).
    cover_image = models.ImageField(upload_to="covers/", blank=True)
    # Focal point + zoom for cropped renders (DESIGN §13): the collection
    # grid tiles are square (object-fit: cover) and chop wide/tall covers
    # at the center by default — the focus percentages feed object-position
    # so the user picks WHICH part survives the crop (50/50 = center), and
    # cover_zoom (50–300 %) scales the tile img toward that point
    # (transform-origin) for a tighter crop. Below 100 % the tile switches
    # to "fit": the whole art shows contain-style over the chosen
    # letterbox colour (see cover_fit / cover_fit_color).
    cover_focus_x = models.PositiveSmallIntegerField(default=50)
    cover_focus_y = models.PositiveSmallIntegerField(default=50)
    cover_zoom = models.PositiveSmallIntegerField(default=100)
    # Fit-mode letterbox colour, picked in the cover editor ("#rrggbb",
    # validated in the view). The user chose a solid colour over auto
    # blur/feather treatments (issue #1 follow-ups) — blurred underlays
    # kept either framing the art in murk or eating its edges. Blank = no
    # backdrop, the plain tile background shows.
    cover_fit_color = models.CharField(max_length=7, blank=True)
    # Local cover art dimensions in px, recorded wherever the file is
    # written (cover replace, download_covers — which also backfills them
    # for files it skips). They exist so fit-mode zoom can interpolate
    # aspect-ratio-aware instead of jumping at the cover→contain switch
    # (issue #1). None when no local file was written yet — cover_scale
    # falls back to the plain zoom/100 factor there. NOT ImageField
    # width/height_field: its post_init hook raises on rows whose file
    # went missing.
    cover_width = models.PositiveIntegerField(null=True, blank=True)
    cover_height = models.PositiveIntegerField(null=True, blank=True)
    # Baked square grid-tile derivative (issue #104): the collection grid
    # tile as it renders — focus point, zoom and fit/letterbox colour all
    # applied server-side into a small square PNG — so a page of the grid
    # serves lightweight thumbnails instead of the full cover_image cropped
    # in CSS. A pure derivative, regenerated wherever the inputs change
    # (cover replace, focus/zoom/fit edit, download_covers) and backfilled
    # by the generate_cover_previews command. cover_image stays the source
    # of truth for the editor and detail heroes.
    cover_preview = models.ImageField(upload_to="covers/previews/", blank=True)

    class Meta:
        abstract = True

    @property
    def cover_fit(self):
        """True when cover_zoom dips below 100 (§13 zoom-out): grid tiles
        and the crop preview show the whole art contain-style over the
        chosen letterbox colour instead of cropping square. Grid tiles
        only — detail-page heroes stay plain."""
        return self.cover_zoom < 100

    @property
    def cover_scale(self):
        """cover_zoom as a unitless CSS scale() factor ("1.3"). Percentages
        inside scale() need newer browsers; a plain number does not. A
        pre-formatted string so template localization can never render a
        decimal comma into the CSS.

        Fit mode (zoom < 100) with known art dimensions interpolates
        aspect-ratio-aware (issue #1): object-fit contain already shrinks
        non-square art by its aspect ratio versus the square cover crop,
        so a plain zoom/100 jumps at the 100 boundary. s(z) = 1 +
        (AR−1)·(z−50)/50 — s(just under 100) ≈ the cover-crop size,
        s(50) = the exact contain fit. Unknown dimensions (remote-only
        covers) keep the old plain factor."""
        if self.cover_fit and self.cover_width and self.cover_height:
            ratio = (max(self.cover_width, self.cover_height)
                     / min(self.cover_width, self.cover_height))
            scale = 1 + (ratio - 1) * (self.cover_zoom - 50) / 50
            return f"{round(scale, 3):g}"
        return f"{self.cover_zoom / 100:g}"

    @property
    def cover_url(self):
        return self.cover_image.url if self.cover_image else ""

    @property
    def cover_preview_url(self):
        """URL of the baked square grid thumbnail (issue #104), "" when none
        has been rendered yet — the grid tile falls back to the full cover."""
        return self.cover_preview.url if self.cover_preview else ""

    def regenerate_cover_preview(self, *, save=True):
        """Re-bake the square grid-tile preview from the current cover_image
        + focus/zoom/fit fields (issue #104). No local cover => clear any
        stale preview instead."""
        if not self.cover_image:
            self.clear_cover_preview(save=save)
            return
        with self.cover_image.open("rb") as handle:
            data = handle.read()
        preview = render_square_preview(
            data, self.cover_focus_x, self.cover_focus_y,
            self.cover_zoom, self.cover_fit_color)
        stem = PurePosixPath(self.cover_image.name).stem
        # Content-address the filename (issue #116): the token is a hash of the
        # rendered pixels, so the URL changes exactly when the image changes and
        # a URL never maps to two different renders — which lets nginx serve
        # previews `immutable` for aggressive browser caching. (Django's plain
        # suffix churn would resurrect a base name after a delete, which is
        # unsafe once immutable.)
        token = hashlib.sha256(preview).hexdigest()[:16]
        new_name = f"{stem}-{token}.png"
        old_name = self.cover_preview.name
        if old_name and PurePosixPath(old_name).name == new_name:
            return  # identical content — keep the cached URL, don't rewrite
        self.cover_preview.save(new_name, ContentFile(preview), save=save)
        if old_name and old_name != self.cover_preview.name:
            self.cover_preview.storage.delete(old_name)

    def clear_cover_preview(self, *, save=True):
        """Drop the baked preview file and field (issue #104), e.g. when the
        cover art is cleared."""
        if self.cover_preview:
            self.cover_preview.delete(save=save)


class Game(CoverArtModel):
    """A title. Base game or expansion; identity via BggLink (DESIGN §4).

    BGG-synced fields are nullable and populated later by the sync engine (§8).
    Curated taxonomy (§10) lives partly here (first-class fields), partly in
    Tag / GameType / DigitalImplementation.
    """

    class Type(models.TextChoices):
        BASE = "base", "Base game"
        EXPANSION = "expansion", "Expansion"

    class LanguageDependency(models.TextChoices):
        # Collapsed language-dependency / difficulty-for-non-speakers scale
        # (issue #2): the sheet's two columns describe the same axis, so
        # they're merged (non-speaker value wins when both are set — see
        # import_taxonomy.py's merge rule).
        NO_TEXT = "no_text", "No text"
        TRIVIAL = "trivial", "Trivial"
        EASY = "easy", "Easy"
        MEDIUM = "medium", "Medium"
        DIFFICULT = "difficult", "Difficult"

    class AppUse(models.TextChoices):
        REQUIRED = "required", "Required"
        OPTIONAL = "optional", "Optional"

    class WishlistPriority(models.IntegerChoices):
        # BGG's own 5-level wishlistpriority scale (models.py bgg_wishlist_priority
        # docstring below; also reused by WishlistEntry.priority, issue #64).
        MUST_HAVE = 1, "Must have"
        LOVE_TO_HAVE = 2, "Love to have"
        LIKE_TO_HAVE = 3, "Like to have"
        THINKING_ABOUT_IT = 4, "Thinking about it"
        DONT_BUY = 5, "Don't buy this"

    class BggCollectionStatus(models.TextChoices):
        OWN = "own", "Owned"
        PREORDERED = "preordered", "Preordered"
        PREV_OWNED = "prev_owned", "Previously owned"
        # Wishlisted-but-not-owned. Only applied to games ALREADY in the app
        # (§8): the sync never suggests adding wishlist-only BGG items — a
        # wishlist is aspirational, not a collection to mirror.
        WISHLIST = "wishlist", "Wishlist"

    # User-facing title. May differ from the BGG canonical name (the sheet keeps
    # localized alt-names, e.g. "6 nimmt! (6 bere!)"), so it is stored separately
    # from bgg_name below.
    name = models.CharField(max_length=300)
    # Derived sort key (issue #6): name minus a leading article, maintained
    # by save() — "The Crew" files under C on every name-ordered surface.
    sort_name = models.CharField(max_length=300, editable=False, blank=True)
    type = models.CharField(max_length=20, choices=Type.choices, default=Type.BASE)

    # Expansion relationships (DESIGN §4). Non-symmetrical self M2M: an expansion
    # can expand several bases; a base has many expansions. Populated from BGG
    # (the Overview sheet only records an expansion *count*, not identities).
    expands = models.ManyToManyField(
        "self", symmetrical=False, blank=True, related_name="expansions",
    )
    # Optional per-expansion stat overrides (DESIGN §4). Only meaningful when
    # type=expansion; effective per-copy stats are computed in logic later.
    players_min_override = models.PositiveIntegerField(null=True, blank=True)
    players_max_override = models.PositiveIntegerField(null=True, blank=True)
    playtime_delta_override = models.IntegerField(
        null=True, blank=True, help_text="Minutes this expansion adds (may be negative).",
    )

    # Series membership (DESIGN §4, issue #21): single-membership FK — a game
    # collapses into at most one grid tile. SET_NULL: deleting a Series just
    # releases its members back to individual tiles.
    series = models.ForeignKey(
        "Series", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="members",
    )

    # Family membership (DESIGN §4, issue #42): loose M2M — a game may sit
    # in several families, and families never collapse the grid (that is
    # Series' job). related_name mirrors Series.members; different target
    # model, so no clash.
    families = models.ManyToManyField(
        "Family", blank=True, related_name="members",
    )

    # Designer credits (issue #19), populated from BGG's boardgamedesigner
    # thing-payload links. Plain M2M — no per-link data like GameTag's
    # is_favourite, so no through model.
    designers = models.ManyToManyField(
        "Designer", blank=True, related_name="games",
    )

    # §7 documents (rulebooks, references) attach here via the generic
    # relation, so obj.documents works and deleting the game clears them.
    documents = GenericRelation("Document")

    # --- BGG-synced fields (§8 fills these; blank until first sync) ---
    bgg_name = models.CharField(max_length=300, blank=True)
    year_published = models.IntegerField(null=True, blank=True)
    image_url = models.URLField(max_length=500, blank=True)
    thumbnail_url = models.URLField(max_length=500, blank=True)
    # cover_image + focus/zoom/fit fields come from CoverArtModel. The local
    # file is fetched by download_covers (re-runs never overwrite) or hand-
    # replaced via /games/<pk>/cover/; the grid prefers it over the remote
    # BGG URLs, so covers survive BGG outages.
    min_players = models.PositiveIntegerField(null=True, blank=True)
    max_players = models.PositiveIntegerField(null=True, blank=True)
    min_playtime = models.PositiveIntegerField(null=True, blank=True)
    max_playtime = models.PositiveIntegerField(null=True, blank=True)
    weight = models.DecimalField(max_digits=4, decimal_places=2, null=True, blank=True)
    bgg_rank = models.IntegerField(null=True, blank=True)
    bgg_rating = models.DecimalField(max_digits=5, decimal_places=3, null=True, blank=True)
    # The owner's logged play count for this game, from the collection
    # payload's <numplays> (§8 — token-free, unlike weight). Like
    # bgg_collection_status this is the single synced BGG account's figure
    # (per-user plays wait for multi-user syncing). NULL means unsynced or
    # zero — the UI only surfaces a positive count.
    bgg_numplays = models.PositiveIntegerField(null=True, blank=True)
    # How the owner's BGG collection lists this game (§8). When BGG sets
    # several flags at once, own beats preordered beats previously-owned.
    # Blank = not in the BGG collection (or not synced yet). A previously-
    # owned game STAYS in the app — the UI marks it, never hides it.
    bgg_collection_status = models.CharField(
        max_length=20, choices=BggCollectionStatus.choices, blank=True,
    )
    # BGG wishlist priority, from the <status wishlistpriority="..."> attribute.
    # NULL when not wishlisted.
    bgg_wishlist_priority = models.PositiveSmallIntegerField(
        null=True, blank=True, choices=WishlistPriority.choices,
    )
    last_synced_at = models.DateTimeField(null=True, blank=True)

    # What we last pushed up to BGG (issue #117), and when. "" = "remove from
    # collection" was pushed. The read-sync honours this within
    # bgg_sync.PUSH_CONFIRM_WINDOW so a not-yet-propagated write isn't
    # stale-cleared or diff-flagged; cleared once a read confirms it (or the
    # window lapses). Deliberately NOT in BGG_SYNCED_FIELDS — a read sync
    # never writes these, only push_bgg_status does.
    bgg_status_pushed = models.CharField(
        max_length=20, choices=BggCollectionStatus.choices, blank=True,
    )
    bgg_status_pushed_at = models.DateTimeField(null=True, blank=True)

    # Issue #82: last-pushed "for trade" flag, tracked separately from
    # bgg_status_pushed above — BGG's fortrade flag is orthogonal to the
    # single membership status (a for-trade copy is still "own"), so it is
    # merged into the collection item's status on push rather than replacing
    # it, and needs its own push bookkeeping.
    bgg_fortrade_pushed = models.BooleanField(default=False)
    bgg_fortrade_pushed_at = models.DateTimeField(null=True, blank=True)

    # --- §10 taxonomy ---
    # Mechanics (kind=mechanic, BGG-synced once the registered-app token
    # exists) and themes (kind=theme, the user's own vocabulary) share this
    # M2M; the through model carries the sheet's favourite-theme mark.
    tags = models.ManyToManyField(
        "Tag", through="GameTag", related_name="games", blank=True,
    )

    # Campaign structure (DESIGN §10). Four booleans, not one choice: the
    # sheet freely combines them (Scenarios + One-off is the norm for
    # scenario games that also play standalone).
    is_campaign = models.BooleanField(default=False)
    is_legacy = models.BooleanField(default=False, help_text="Evolving / Legacy.")
    has_scenarios = models.BooleanField(default=False, help_text="Scenarios / missions.")
    is_one_off = models.BooleanField(default=False)

    # Language (DESIGN §10). Component language is per-Edition (a Czech
    # edition differs); these two describe the game itself.
    language_dependency = models.CharField(
        max_length=20, choices=LanguageDependency.choices, blank=True,
    )
    language_dependency_note = models.CharField(
        max_length=300, blank=True,
        help_text='The raw sheet detail, e.g. "easy (goals only, coop)".',
    )

    # App & soundtrack (DESIGN §10).
    companion_app = models.CharField(
        max_length=20, choices=AppUse.choices, blank=True,
        help_text="Does playing use a companion app?",
    )
    has_app_version = models.BooleanField(
        default=False, help_text="A digital version of the game exists.",
    )
    soundtrack_ambience = models.BooleanField(default=False)
    soundtrack_timer = models.BooleanField(default=False)

    # Player conflict (DESIGN §10): single 0–3 value + note in v1; per-mode
    # variant modeling is deferred (§14). Messy sheet values ("0-1?") keep
    # the value null and live in the note.
    player_conflict = models.PositiveSmallIntegerField(
        null=True, blank=True, help_text="0 (none) – 3 (heavy).",
    )
    player_conflict_note = models.CharField(max_length=300, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["sort_name", "name"]

    def __str__(self):
        return self.name

    @staticmethod
    def compute_sort_name(name):
        """The article-blind, case-blind sort key (issue #6). A name that IS
        just an article ("A", weirdly) keeps itself rather than sorting as
        ""."""
        stripped = _LEADING_ARTICLE.sub("", name.strip())
        return (stripped or name).lower()

    def save(self, *args, **kwargs):
        self.sort_name = self.compute_sort_name(self.name)
        # sync_bgg saves with update_fields — a rename there must carry the
        # recomputed key along.
        update_fields = kwargs.get("update_fields")
        if update_fields is not None and "name" in update_fields:
            kwargs["update_fields"] = [*update_fields, "sort_name"]
        super().save(*args, **kwargs)

    @property
    def primary_base(self):
        """The base game an expansion's documents nest under (issue #99), or
        None for base games and unlinked expansions. Multi-base expansions
        pick the *first linked* base (lowest pk / insertion order) — stable
        against a base being renamed, so the expansion's document subtree
        doesn't relocate just because a sibling base changed its title."""
        if self.type != self.Type.EXPANSION:
            return None
        return self.expands.order_by("pk").first()

    @property
    def short_name(self):
        """Issue #98: the expansion-specific tail of the title, with a linked
        base game's name + separator stripped, for surfaces that already show
        the base game. Falls back to the full name for base games, expansions
        with no linked base, or titles that don't start with a base name.
        Prefetch "expansions__expands" (base pages) to avoid N+1."""
        if self.type != self.Type.EXPANSION:
            return self.name
        for base in self.expands.all():
            if base.name and self.name.startswith(base.name):
                rest = self.name[len(base.name):]
                for sep in _EXPANSION_SEPARATORS:
                    if rest.startswith(sep):
                        return rest[len(sep):]
        return self.name

    @property
    def primary_bgg_link(self):
        return self.bgg_links.filter(is_primary=True).first()

    @property
    def cover_url(self):
        """Best available cover (DESIGN §13): the local file, else the
        full-size remote image, else the thumbnail. Empty when unsynced."""
        if self.cover_image:
            return self.cover_image.url
        return self.image_url or self.thumbnail_url

    @property
    def is_owned(self):
        """True when this game has at least one active, truly-owned
        (non-archived, not borrowed-in) Copy — the "owned" surface the shared
        grid and effective stats use, distinct from merely sitting in the DB
        (a preordered or BGG-synced-but-unowned game has no Copy). A
        borrowed-in copy (issue #43) is present but not owned, so it doesn't
        count here. Prefetch "editions__copies" to avoid N+1."""
        return any(
            copy.archive_status == Copy.ArchiveStatus.ACTIVE and not copy.is_borrowed_in
            for edition in self.editions.all()
            for copy in edition.copies.all()
        )

    @property
    def has_pnp_edition(self):
        """True when any edition of this game is print-and-play (DESIGN §6).
        The is_pnp flag lives on Edition; this re-derives the game-level "is any
        copy of this a PnP?" view. Prefetch "editions" to avoid N+1."""
        return any(edition.is_pnp for edition in self.editions.all())

    def effective_player_range(self):
        """(min, max) players including OWNED expansions' overrides (DESIGN §4).

        For an expansion, its own overrides beat its synced stats. For a base
        game, only expansions someone actually owns (an active Copy exists)
        widen the range — a preordered or BGG-synced-but-unowned expansion in
        the DB does not (DESIGN §4: your effective range comes from expansions
        that are owned, not from every expansion that exists). Prefetch
        "expansions__editions__copies" to avoid N+1. Either bound is None when
        nothing (BGG sync or overrides) provided it.
        """
        low = self.players_min_override or self.min_players
        high = self.players_max_override or self.max_players
        for expansion in self.expansions.all():
            if not expansion.is_owned:
                continue
            if expansion.players_min_override and (low is None or expansion.players_min_override < low):
                low = expansion.players_min_override
            if expansion.players_max_override and (high is None or expansion.players_max_override > high):
                high = expansion.players_max_override
        return low, high

    def effective_game_types(self):
        """Game-type marks including OWNED expansions' marks (DESIGN §10),
        the taxonomy parallel to effective_player_range.

        Unions this game's own GameType marks with those of every expansion
        someone actually owns (an active Copy exists) — the same owned-only
        rule the player range uses; an unowned (preordered / BGG-synced)
        expansion in the DB does not contribute. When the same game_type
        appears with differing qualifiers, the least-restrictive one wins
        (native/blank beats 'opt' beats 'app'). Prefetch
        "expansions__editions__copies", "game_types" and
        "expansions__game_types" to avoid N+1.

        Returns a list of dicts (ordered by GameType.Type.choices), each:
        game_type / display / qualifier / qualifier_display / from_expansion —
        from_expansion is True when the base game carries no native mark of
        that type, i.e. it surfaces only because an owned expansion provides
        it (drives the provenance styling on the detail page).
        """
        rank = {"": 0, GameType.Qualifier.OPTIONAL: 1, GameType.Qualifier.APP: 2}
        winning = {}   # game_type -> least-restrictive qualifier seen
        native = set()  # game_types the base game itself marks
        for pt in self.game_types.all():
            native.add(pt.game_type)
            winning[pt.game_type] = pt.qualifier
        for expansion in self.expansions.all():
            if not expansion.is_owned:
                continue
            for pt in expansion.game_types.all():
                if pt.game_type not in winning or rank[pt.qualifier] < rank[winning[pt.game_type]]:
                    winning[pt.game_type] = pt.qualifier
        result = []
        for value, label in GameType.Type.choices:
            if value not in winning:
                continue
            qualifier = winning[value]
            result.append({
                "game_type": value,
                "display": label,
                "qualifier": qualifier,
                "qualifier_display": GameType.Qualifier(qualifier).label if qualifier else "",
                "from_expansion": value not in native,
            })
        return result


class BggLink(models.Model):
    """A BGG id attached to a Game (DESIGN §4). Stores only the id; the URL is
    derived from one template. Exactly one link per Game is primary and drives
    synced stats/image."""

    game = models.ForeignKey(Game, on_delete=models.CASCADE, related_name="bgg_links")
    bgg_id = models.PositiveIntegerField()
    is_primary = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["game", "bgg_id"], name="unique_bgg_id_per_game",
            ),
            # At most one primary link per game.
            models.UniqueConstraint(
                fields=["game"], condition=models.Q(is_primary=True),
                name="unique_primary_bgg_link_per_game",
            ),
        ]

    def __str__(self):
        return f"BGG {self.bgg_id}" + (" (primary)" if self.is_primary else "")

    @property
    def url(self):
        return BGG_THING_URL_TEMPLATE.format(id=self.bgg_id)


class Play(models.Model):
    """A single recorded play of a Game (DESIGN §8), pulled read-only from BGG.

    GameKeeper is explicitly NOT a play-logger: plays are authored in BG Stats
    (which auto-posts to BGG) and this app only reads and displays them. The
    always-on cheap signal is the `Game.bgg_numplays` COUNT; this model is the
    richer per-play history (date / location / players / scores / winners).

    `source` keeps the row provider-agnostic so the future BG Stats JSON importer
    can ENRICH the same history without a rewrite: BGG plays land as source=BGG
    keyed on the <play id>; BG Stats plays would land as source=BG_STATS keyed on
    their uuid. `(source, external_id)` is unique so a re-sync upserts in place
    (idempotent) rather than duplicating.
    """

    class Source(models.TextChoices):
        BGG = "bgg", "BGG"
        BG_STATS = "bgstats", "BG Stats"

    game = models.ForeignKey(Game, on_delete=models.CASCADE, related_name="plays")
    source = models.CharField(
        max_length=20, choices=Source.choices, default=Source.BGG,
    )
    # Stable per-source play id: the BGG <play id> now, a BG Stats uuid later.
    external_id = models.CharField(max_length=64)
    play_date = models.DateField(null=True, blank=True)
    quantity = models.PositiveIntegerField(default=1)
    length_minutes = models.PositiveIntegerField(null=True, blank=True)
    location = models.CharField(max_length=200, blank=True)
    incomplete = models.BooleanField(default=False)
    comments = models.TextField(blank=True)
    synced_at = models.DateTimeField()

    class Meta:
        ordering = ["-play_date", "-external_id"]
        constraints = [
            models.UniqueConstraint(
                fields=["source", "external_id"], name="unique_play_per_source",
            ),
        ]

    def __str__(self):
        return f"{self.game.name} played {self.play_date or '?'}"


class PlayPlayer(models.Model):
    """One participant in a Play (DESIGN §8). All fields are read-only BGG data;
    `score`/`rating` stay free-text because BGG permits non-numeric scores and
    leaves them blank for score-less games."""

    play = models.ForeignKey(Play, on_delete=models.CASCADE, related_name="players")
    name = models.CharField(max_length=200, blank=True)
    username = models.CharField(max_length=100, blank=True)
    score = models.CharField(max_length=50, blank=True)
    won = models.BooleanField(default=False)
    is_new = models.BooleanField(default=False)
    color = models.CharField(max_length=100, blank=True)
    start_position = models.CharField(max_length=50, blank=True)
    rating = models.CharField(max_length=50, blank=True)

    class Meta:
        ordering = ["start_position", "name"]

    def __str__(self):
        return self.name or self.username or "(anonymous player)"


class ExternalLink(models.Model):
    """A generic non-BGG link on a Game (DESIGN §4): (type, url, label).

    If the type has a URL template we store an id and derive the URL; otherwise
    (e.g. zatrolené-hry.cz's ugly slugs) we store the full pasted URL.
    """

    class LinkType(models.TextChoices):
        KICKSTARTER = "kickstarter", "Kickstarter"
        GAMEFOUND = "gamefound", "Gamefound"
        ZATROLENE = "zatrolene", "Zatrolené hry"
        GOOGLE_DRIVE = "drive", "Google Drive"
        DROPBOX = "dropbox", "Dropbox"
        OTHER = "other", "Other"

    # Types whose URL can be derived from an id. Types absent here store a full
    # URL in `url` instead (add a template string when one becomes known).
    URL_TEMPLATES: dict[str, str] = {}

    game = models.ForeignKey(
        Game, on_delete=models.CASCADE, related_name="external_links",
    )
    link_type = models.CharField(max_length=30, choices=LinkType.choices)
    external_id = models.CharField(max_length=200, blank=True)
    url = models.URLField(max_length=1000, blank=True)
    label = models.CharField(max_length=200, blank=True)

    def __str__(self):
        return self.label or self.get_link_type_display()

    @property
    def resolved_url(self):
        template = self.URL_TEMPLATES.get(self.link_type)
        if template and self.external_id:
            return template.format(id=self.external_id)
        return self.url


# ===========================================================================
# §4  Series
# ===========================================================================

class Series(CoverArtModel):
    """A group of near-interchangeable games (DESIGN §4, issue #21):
    (near-)identical rules, stats and duration — "the MicroMacro series".

    Display layer ONLY: the collection grid collapses members into one tile
    and the series detail page lists a union over members, but Copies,
    Editions and sleeve requirements stay per-member, never merged.

    Cover machinery (cover_image + focus/zoom/fit, issue #54) comes from
    CoverArtModel: the optional custom tile art gets the full editor
    treatment; when unset the primary member's cover represents the series
    (see cover_source). The inherited cover_url stays local-file-or-"" —
    it must NOT fall back to the primary, cover_source owns that choice.
    """

    name = models.CharField(max_length=300)
    # The member whose cover and stats represent the series. PROTECT: forces
    # repointing/deleting the series before its representative game can go —
    # SET_NULL would need fallback logic in every cover/stat consumer.
    primary_game = models.ForeignKey(
        Game, on_delete=models.PROTECT, related_name="primary_of_series",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "series"

    def __str__(self):
        return self.name

    @property
    def cover_source(self):
        """Object whose cover machinery renders the collapsed tile: this
        series when a custom cover is set, else the primary member."""
        return self if self.cover_image else self.primary_game

    def clean(self):
        # Guards admin edits (ModelForm runs full_clean); the in-app editor
        # validates in the view. Only meaningful once members exist —
        # membership is assigned after the row does, so create is exempt.
        if self.pk and self.members.exists() \
                and not self.members.filter(pk=self.primary_game_id).exists():
            raise ValidationError("Primary game must be one of the members.")


class Family(CoverArtModel):
    """A loose association of distinct-but-related games (DESIGN §4, issue
    #42): same designer/world, genuinely different gameplay — "the Burgle
    Bros family". The counterpart to Series: membership is an M2M
    (Game.families — a game may sit in a designer line AND a theme grouping)
    and it NEVER collapses the collection grid. Surfaces as a "part of
    family" section on member detail pages and a GameChooser facet.

    BGG's "family" taxonomy corresponds to this entity, hence bgg_family_id;
    auto-seeding waits for the registered-app token (§15) — manual curation
    is primary. Cover machinery (issue #54) is inherited for the family
    detail hero and the future families overview. The inherited cover_url
    stays local-file-or-"" — cover_source owns the fallback: no designated
    primary member here, so it is the first member alphabetically."""

    name = models.CharField(max_length=300)
    # BGG family id (boardgamegeek.com/boardgamefamily/<id>). Optional: many
    # curated families are the user's own vocabulary with no BGG row.
    bgg_family_id = models.PositiveIntegerField(null=True, blank=True)
    # Free-form curation note ("Fowers heist line — BB2 is the heavier one").
    note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "families"

    def __str__(self):
        return self.name

    @property
    def cover_source(self):
        """Object whose cover machinery renders the hero (and the future
        overview tile): this family when custom art is set, else the first
        member alphabetically (Game Meta order — sort_name is article-blind,
        issue #6). None when there are no members either."""
        if self.cover_image:
            return self
        members = list(self.members.all())  # walks the prefetch when warm
        return members[0] if members else None


class Designer(models.Model):
    """A game designer credit (issue #19), shared across every game they
    worked on via Game.designers. Unlike Tag(kind=mechanic), BGG's
    boardgamedesigner links carry a stable id, so dedupe keys on
    bgg_designer_id rather than name."""

    name = models.CharField(max_length=300)
    bgg_designer_id = models.PositiveIntegerField(unique=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


# ===========================================================================
# §10  Taxonomy: tags, game types, digital implementations
# ===========================================================================

class Tag(models.Model):
    """A mechanic or theme label on Games (DESIGN §10).

    kind=theme is the user's own curated vocabulary (adapts-from folds in as
    "Adapts: Book" etc.); kind=mechanic is the seam for the BGG sync — empty
    until the registered-app token unblocks xmlapi2/thing (§15).
    """

    class Kind(models.TextChoices):
        MECHANIC = "mechanic", "Mechanic"
        THEME = "theme", "Theme"

    kind = models.CharField(max_length=20, choices=Kind.choices)
    name = models.CharField(max_length=100)

    class Meta:
        ordering = ["kind", "name"]
        constraints = [
            models.UniqueConstraint(fields=["kind", "name"], name="unique_tag_per_kind"),
        ]

    def __str__(self):
        return f"{self.name} ({self.kind})"


class GameTag(models.Model):
    """Through model for Game.tags, carrying the sheet's favourite mark
    (an 'f' instead of 'y' in a theme cell)."""

    game = models.ForeignKey(Game, on_delete=models.CASCADE, related_name="game_tags")
    tag = models.ForeignKey(Tag, on_delete=models.CASCADE, related_name="game_tags")
    is_favourite = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["game", "tag"], name="unique_tag_per_game"),
        ]

    def __str__(self):
        return f"{self.game}: {self.tag}" + (" ★" if self.is_favourite else "")


class GameType(models.Model):
    """One game-type mark on a Game (DESIGN §10 multi-select), with the
    sheet's qualifier: plain 'y', 'opt' (optional, e.g. optional solo mode)
    or 'app' (only with the companion app).

    Note on SOLO (issue #124): unlike the other values it describes player
    *count*, not how players interact, so it looks derivable from
    ``min_players == 1``. It is kept deliberately because it records two things
    the player-count range cannot: a *designed solo mode for a 2+ player game*
    (e.g. Pandemic's solo variant) and the native / optional ('opt') /
    app-only ('app') distinction. Don't drop it without replacing both."""

    class Type(models.TextChoices):
        ONE_VS_ALL = "1vsall", "1 vs All"
        COMPETITIVE = "competitive", "Competitive"
        COOPERATIVE = "cooperative", "Cooperative"
        SEMI_COOP = "semi_coop", "Semi-coop"
        SOLO = "solo", "Solo"
        TEAM = "team", "Team"
        TRAITOR = "traitor", "Traitor"

    class Qualifier(models.TextChoices):
        OPTIONAL = "opt", "Optional"
        APP = "app", "With app only"

    game = models.ForeignKey(Game, on_delete=models.CASCADE, related_name="game_types")
    game_type = models.CharField(max_length=20, choices=Type.choices)
    qualifier = models.CharField(max_length=10, choices=Qualifier.choices, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["game", "game_type"], name="unique_game_type_per_game",
            ),
        ]

    def __str__(self):
        label = self.get_game_type_display()
        return f"{self.game}: {label}" + (f" ({self.qualifier})" if self.qualifier else "")


class AlternateName(models.Model):
    """Issue #51: a hand-curated alternate/localized title for a Game (e.g.
    Beasty Bar → Safari Bar in Czech). Shown on the detail page and matched by
    the collection search box. Names are manual-only — BGG's dozens of
    <name type="alternate"> entries are not imported."""

    game = models.ForeignKey(
        Game, on_delete=models.CASCADE, related_name="alternate_names",
    )
    name = models.CharField(max_length=300)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["game", "name"], name="unique_alternate_name_per_game",
            ),
        ]

    def __str__(self):
        return f"{self.game}: {self.name}"


class DigitalImplementation(models.Model):
    """A digital version of a Game on a platform (DESIGN §10, from the old
    APPs sheet). The sheet carries only platform flags, so url is optional."""

    class Platform(models.TextChoices):
        ANDROID = "android", "Android"
        STEAM = "steam", "Steam"
        BGA = "bga", "Board Game Arena"
        OTHER = "other", "Other"

    game = models.ForeignKey(
        Game, on_delete=models.CASCADE, related_name="digital_implementations",
    )
    platform = models.CharField(max_length=20, choices=Platform.choices)
    url = models.URLField(max_length=500, blank=True)
    notes = models.CharField(max_length=300, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["game", "platform"], name="unique_platform_per_game",
            ),
        ]

    def __str__(self):
        return f"{self.game} on {self.get_platform_display()}"


# ===========================================================================
# §9  Location  (a Copy points at its current location)
# ===========================================================================

class Location(models.Model):
    """A named place a Copy can live (DESIGN §9). Movement history comes from
    simple-history on Copy. Lending (issue #43) is tracked separately via
    Loan, independent of Location — a copy's physical spot and its loan
    status are orthogonal."""

    class Type(models.TextChoices):
        STORAGE = "storage", "Storage"
        OTHER = "other", "Other"

    group = models.ForeignKey(
        Group, on_delete=models.CASCADE, related_name="locations",
    )
    name = models.CharField(max_length=200)
    type = models.CharField(max_length=20, choices=Type.choices, default=Type.STORAGE)
    # Issue #123: unguessable token for a share link scoped to just this
    # location (DESIGN §3 tier-4 style, but per-Location instead of per-Group).
    # Null = no location-scoped link.
    share_token = models.CharField(
        max_length=64, unique=True, null=True, blank=True,
        help_text="Unguessable token for the location-scoped share link.",
    )

    def __str__(self):
        return self.name

    def enable_share_link(self):
        """Mint the location-scoped share token (mirrors
        Group.enable_share_link) if absent. Revoking = blanking share_token."""
        if not self.share_token:
            self.share_token = secrets.token_urlsafe(32)
            self.save(update_fields=["share_token"])
        return self.share_token


class Loan(models.Model):
    """A copy currently on loan — either lent out to someone else or
    borrowed in from someone else (DESIGN §9, issue #43). Independent of
    Location: lending a copy out no longer requires a dedicated per-person
    Location. Exactly one of counterparty_user / counterparty_name is set
    (mirrors ShareGrant's exactly-one-grantee pattern), since the other party
    may or may not be a registered app user. Rows are kept (returned_at set,
    not deleted) so a copy's loan history survives across relends."""

    class Direction(models.TextChoices):
        LENT_OUT = "lent_out", "Lent out"
        BORROWED_IN = "borrowed_in", "Borrowed in"

    copy = models.ForeignKey("Copy", on_delete=models.CASCADE, related_name="loans")
    direction = models.CharField(max_length=20, choices=Direction.choices)
    counterparty_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="loans",
    )
    counterparty_name = models.CharField(max_length=200, blank=True)
    since = models.DateField(null=True, blank=True)
    expected_return_date = models.DateField(null=True, blank=True)
    returned_at = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(counterparty_user__isnull=False, counterparty_name="")
                    | models.Q(counterparty_user__isnull=True, counterparty_name__gt="")
                ),
                name="loan_exactly_one_counterparty",
            ),
            models.UniqueConstraint(
                fields=["copy"], condition=models.Q(returned_at__isnull=True),
                name="loan_one_active_per_copy",
            ),
        ]

    def __str__(self):
        who = self.counterparty_user or self.counterparty_name
        return f"{self.get_direction_display()}: {self.copy} ({who})"

    @property
    def counterparty(self):
        return self.counterparty_user or self.counterparty_name


# ===========================================================================
# §4  Edition  (a BGG "version": base / Collector's / Anniversary)
# ===========================================================================

class Edition(models.Model):
    """A version of a Game (DESIGN §4). Lightweight; a default auto-edition
    exists when editions don't matter, and a Copy always points at one.
    Physical attributes (which differ per edition) live here."""

    class SizeCategory(models.TextChoices):
        # From the Overview "Size" column: T/S/M/N/L/H.
        TINY = "tiny", "Tiny"
        SMALL = "small", "Small"
        MEDIUM = "medium", "Medium"
        NORMAL = "normal", "Normal"
        LARGE = "large", "Large"
        HUGE = "huge", "Huge"

    class ComponentsLanguage(models.TextChoices):
        # The sheet's "Lang (components)" vocabulary. Per-Edition, not
        # per-Game (§10): a Czech edition is a different edition.
        ENGLISH = "en", "English"
        CZECH = "cs", "Czech"
        CZECH_ENGLISH = "cs_en", "Czech + English"
        NONE = "none", "No language on components"

    game = models.ForeignKey(Game, on_delete=models.CASCADE, related_name="editions")
    name = models.CharField(
        max_length=200, blank=True,
        help_text='Edition name, e.g. "Kickstarter Edition". Blank for the default.',
    )
    # §7 documents (edition-specific references) — generic relation, UI later.
    documents = GenericRelation("Document")
    is_default = models.BooleanField(default=False)
    # Print-and-play flag (DESIGN §6): a property of the edition/copy, not the
    # title — you can own the same game as a PnP copy and a store copy at once.
    # PnP editions still count for sleeves.
    is_pnp = models.BooleanField(default=False)
    bgg_version_id = models.PositiveIntegerField(null=True, blank=True)

    # Physical attributes (DESIGN §4 / §9).
    components_language = models.CharField(
        max_length=10, choices=ComponentsLanguage.choices, blank=True,
    )
    size_category = models.CharField(
        max_length=20, choices=SizeCategory.choices, blank=True,
    )
    num_boxes = models.PositiveIntegerField(null=True, blank=True)
    box_length_mm = models.PositiveIntegerField(null=True, blank=True)
    box_width_mm = models.PositiveIntegerField(null=True, blank=True)
    box_height_mm = models.PositiveIntegerField(null=True, blank=True)

    # Sleeve requirements (CardSize -> count, DESIGN §5) attach here via
    # SleeveRequirement (related_name="sleeve_requirements").

    class Meta:
        constraints = [
            # One default edition per game.
            models.UniqueConstraint(
                fields=["game"], condition=models.Q(is_default=True),
                name="unique_default_edition_per_game",
            ),
        ]

    def __str__(self):
        return f"{self.game.name} — {self.name or 'default edition'}"


# ===========================================================================
# §4  Copy  (a user owns a specific Edition)
# ===========================================================================

class Copy(models.Model):
    """An owned copy of an Edition (DESIGN §4). Ownership and all personal /
    physical attributes are per-user; visible to the group but attributed to the
    owner. simple-history records the movement/upgrade log.

    is_borrowed_in (issue #43) denormalizes "this copy has an active
    borrowed-in Loan" so it can be checked as a plain field in queries (the
    owned-stats queries in views.py) without joining to Loan."""

    class KeepStatus(models.TextChoices):
        ALWAYS_KEEP = "always_keep", "Always keep"
        KEEP = "keep", "Keep"
        UNDECIDED = "undecided", "Undecided"
        MIGHT_CYCLE = "might_cycle", "Might cycle"
        WILL_LEAVE = "will_leave", "Will leave"

    class UpgradeStatus(models.TextChoices):
        # Shared vocabulary for the upgrade/customization columns.
        NONE = "none", "—"
        NOT_NECESSARY = "not_necessary", "Not necessary"
        MAYBE = "maybe", "Maybe"
        TODO = "todo", "To-do"
        DONE = "done", "Done"
        INCLUDED = "included", "Included"

    class ArchiveStatus(models.TextChoices):
        ACTIVE = "active", "Active"
        ARCHIVED = "archived", "Archived"

    class ReadyStatus(models.TextChoices):
        # Generic "is this actually playable yet" state (issue #19) — not
        # PnP-specific, so future not-yet-playable reasons (missing pieces,
        # damaged, etc.) reuse this instead of adding another field.
        READY = "ready", "Ready"
        NOT_READY = "not_ready", "Not ready to play"

    class ArchiveReason(models.TextChoices):
        SOLD = "sold", "Sold"
        GIFTED = "gifted", "Gifted"
        LOST = "lost", "Lost"
        CULLED = "culled", "Culled"
        RETURNED = "returned", "Returned to lender"

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="copies",
    )
    edition = models.ForeignKey(Edition, on_delete=models.PROTECT, related_name="copies")
    acquired_date = models.DateField(null=True, blank=True)

    # Issue #43: true while an active (unreturned) Loan with
    # direction=BORROWED_IN exists for this copy — see the Copy docstring.
    is_borrowed_in = models.BooleanField(default=False)

    # --- Curation / culling (DESIGN §11). Excitement replaces rating. ---
    excitement = models.DecimalField(
        max_digits=3, decimal_places=1, null=True, blank=True,
        help_text="0–10; primary cull signal.",
    )
    keep_status = models.CharField(max_length=20, choices=KeepStatus.choices, blank=True)
    immune = models.BooleanField(default=False)
    play_until_or_leaves = models.CharField(max_length=300, blank=True)
    favourite_thing = models.TextField(blank=True)
    brings_extra = models.TextField(blank=True)
    why_might_leave = models.TextField(blank=True)

    # --- Upgrades / customizations (DESIGN §4) ---
    insert_3d = models.CharField(
        max_length=20, choices=UpgradeStatus.choices, default=UpgradeStatus.NONE,
    )
    card_dividers = models.CharField(
        max_length=20, choices=UpgradeStatus.choices, default=UpgradeStatus.NONE,
    )
    accessories_3d = models.CharField(
        max_length=20, choices=UpgradeStatus.choices, default=UpgradeStatus.NONE,
    )
    other_accessories = models.CharField(
        max_length=20, choices=UpgradeStatus.choices, default=UpgradeStatus.NONE,
    )
    # The Overview upgrade columns are free text (status + detail, e.g.
    # "To-do, trap tokens etc" / "Done, Wet-erase pens"). The enums above carry
    # the filterable status; this holds the descriptive detail the import can't
    # fit into an enum.
    upgrades_note = models.TextField(blank=True)

    # --- Location (DESIGN §9) ---
    location = models.ForeignKey(
        Location, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="copies",
    )
    location_note = models.CharField(
        max_length=300, blank=True,
        help_text='Free note, e.g. "stored inside another game\'s box".',
    )

    # --- Archive (DESIGN §4): retained for reference, hidden from active views ---
    archive_status = models.CharField(
        max_length=20, choices=ArchiveStatus.choices, default=ArchiveStatus.ACTIVE,
    )
    archive_reason = models.CharField(
        max_length=20, choices=ArchiveReason.choices, blank=True,
    )
    archive_date = models.DateField(null=True, blank=True)

    # --- Readiness (issue #19): arrived but not yet playable (e.g. an
    # unprinted PnP copy) — distinct from archive_status, which is about
    # whether the copy is still in the household at all. ---
    ready_status = models.CharField(
        max_length=20, choices=ReadyStatus.choices, default=ReadyStatus.READY,
    )

    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Upgrade history (owned Collector's -> upgraded to Anniversary) and the
    # location movement log both fall out of simple-history tracking the FK
    # changes over time (DESIGN §4/§9). Minis (DESIGN §4 seam) bolt on later
    # without a schema rewrite.
    history = HistoricalRecords()

    class Meta:
        ordering = ["edition__game__name"]

    def __str__(self):
        return f"{self.owner}'s copy of {self.edition}"

    @property
    def active_loan(self):
        """This copy's current unreturned Loan, if any (issue #43). Iterates
        a prefetched `loans` list in Python rather than querying, matching
        how is_owned/availability are already computed elsewhere."""
        for loan in self.loans.all():
            if loan.returned_at is None:
                return loan
        return None


# ===========================================================================
# §5  Sleeves
# ===========================================================================

def _format_mm(value):
    """'41.0' -> '41', '57.5' -> '57.5' — for compact size labels."""
    text = f"{value:.1f}"
    return text[:-2] if text.endswith(".0") else text


class CardSize(models.Model):
    """A card dimension in mm (DESIGN §5): what a game's cards *require*.

    Identity is the (width, height) pair — the Mastersheet names the same
    size differently in different places (Teal vs Azur 45x68, Extra large vs
    Bronze 65x100), so name/aliases are display labels, never keys.
    """

    # decimal_places=1 covers the real-world fractions (57.5x89, 50.5x50.5).
    width_mm = models.DecimalField(max_digits=5, decimal_places=1)
    height_mm = models.DecimalField(max_digits=5, decimal_places=1)
    name = models.CharField(max_length=100, blank=True, help_text='Display name, e.g. "Standard".')
    aliases = models.CharField(
        max_length=300, blank=True,
        help_text="Comma-separated alternative names (e.g. Tlama colour names).",
    )

    class Meta:
        ordering = ["width_mm", "height_mm"]
        constraints = [
            models.UniqueConstraint(
                fields=["width_mm", "height_mm"], name="unique_card_size_dimensions",
            ),
        ]

    def __str__(self):
        label = f"{_format_mm(self.width_mm)}×{_format_mm(self.height_mm)}"
        return f"{self.name} ({label})" if self.name else label

    @property
    def alias_list(self):
        return [a.strip() for a in self.aliases.split(",") if a.strip()]


class SleeveProduct(models.Model):
    """A real purchasable sleeve product (DESIGN §5): brand, fits exactly one
    CardSize, finish/back, pack size. All product attributes live here."""

    class Finish(models.TextChoices):
        MATTE = "matte", "Matte"
        GLOSSY = "glossy", "Glossy"

    class Back(models.TextChoices):
        CLEAR = "clear", "Clear"
        COLORED = "colored", "Colored"
        PRINTED = "printed", "Printed"

    # Free text, not an enum: DESIGN §5 explicitly allows custom brands
    # alongside the known ones (Tlama / AT / Gamegenic / ...).
    brand = models.CharField(max_length=100)
    name = models.CharField(
        max_length=200, blank=True, help_text='Product line, e.g. "Diamond Yellow".',
    )
    card_size = models.ForeignKey(
        CardSize, on_delete=models.PROTECT, related_name="products",
    )
    finish = models.CharField(max_length=20, choices=Finish.choices, blank=True)
    back = models.CharField(max_length=20, choices=Back.choices, blank=True)
    pack_size = models.PositiveIntegerField(default=100)
    url = models.URLField(max_length=500, blank=True)

    class Meta:
        ordering = ["brand", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["brand", "name", "card_size"], name="unique_sleeve_product",
            ),
        ]

    def __str__(self):
        label = f"{self.brand} {self.name}".strip()
        return f"{label} ({self.card_size})"


class SleeveRequirement(models.Model):
    """CardSize -> count on an Edition (DESIGN §5): how many cards of each
    size this edition contains. On Edition, not Game — Collector's ≠ base.

    Manual-entry / import-only for now; the BGG card-size scrape pre-fill
    (§5, optional) is deferred with the rest of the §8 BGG work.
    """

    edition = models.ForeignKey(
        Edition, on_delete=models.CASCADE, related_name="sleeve_requirements",
    )
    card_size = models.ForeignKey(
        CardSize, on_delete=models.PROTECT, related_name="requirements",
    )
    count = models.PositiveIntegerField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["edition", "card_size"], name="unique_requirement_per_edition_size",
            ),
        ]

    def __str__(self):
        return f"{self.edition}: {self.count}× {self.card_size}"


class SleeveInventory(models.Model):
    """Owned stock of one SleeveProduct (DESIGN §5): unopened packs plus
    loose leftovers from opened packs.

    Keyed per owner to mirror Copy ownership; pooling inventory across a
    Group later only means relaxing this key (seam, cf. DESIGN §3).
    """

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="sleeve_inventories",
    )
    product = models.ForeignKey(
        SleeveProduct, on_delete=models.CASCADE, related_name="inventories",
    )
    packs = models.PositiveIntegerField(default=0)
    loose = models.PositiveIntegerField(
        default=0, help_text="Loose sleeves left over from opened packs.",
    )

    class Meta:
        verbose_name_plural = "sleeve inventories"
        constraints = [
            models.UniqueConstraint(
                fields=["owner", "product"], name="unique_inventory_per_owner_product",
            ),
        ]

    def __str__(self):
        return f"{self.owner}: {self.packs} packs + {self.loose} loose of {self.product}"

    @property
    def total_sleeves(self):
        return self.packs * self.product.pack_size + self.loose


class CopySleeveStatus(models.Model):
    """Per-Copy, per-CardSize sleeved state (DESIGN §5), optionally recording
    which SleeveProduct was used — reproduces the sheet's per-brand
    breakdown."""

    class Status(models.TextChoices):
        NOT_SLEEVED = "not_sleeved", "Not sleeved"
        TO_SLEEVE = "to_sleeve", "To sleeve"
        SLEEVED = "sleeved", "Sleeved"

    copy = models.ForeignKey(
        Copy, on_delete=models.CASCADE, related_name="sleeve_statuses",
    )
    card_size = models.ForeignKey(
        CardSize, on_delete=models.PROTECT, related_name="copy_statuses",
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.NOT_SLEEVED,
    )
    product = models.ForeignKey(
        SleeveProduct, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="used_on", help_text="Which product was used, if known.",
    )

    class Meta:
        verbose_name_plural = "copy sleeve statuses"
        constraints = [
            models.UniqueConstraint(
                fields=["copy", "card_size"], name="unique_sleeve_status_per_copy_size",
            ),
        ]

    def __str__(self):
        return f"{self.copy} / {self.card_size}: {self.get_status_display()}"


# ===========================================================================
# §16  Accessories
# ===========================================================================

class Accessory(models.Model):
    """A real accessory product (DESIGN §16): playmat, upgraded tokens, 3D
    insert, card dividers, or a standalone add-on. A catalog row, reused
    across users — mirrors Game/Copy and SleeveProduct/SleeveInventory: the
    owned instance is a separate AccessoryCopy.

    Deliberately lean, unlike Game: no BGG rank/numplays/wishlist-priority/
    push-bookkeeping block — accessories don't sync into a BGG "collection"
    the way games do. They do have their own BGG "boardgameaccessory" pages
    though, so a light identity + display block exists (bgg_id/bgg_name/
    image_url/bgg_rating/last_synced_at) — populated by hand in admin for
    now; automatic syncing is deferred to a follow-up issue.

    game/edition are optional and mutually exclusive: set game when the
    accessory applies to every printing (the common BGG-linked case), set
    edition when it's specific to one printing, leave both blank for a
    standalone/generic accessory (e.g. a generic neoprene playmat)."""

    name = models.CharField(max_length=300)
    brand = models.CharField(max_length=100, blank=True)

    game = models.ForeignKey(
        Game, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="accessories",
    )
    edition = models.ForeignKey(
        Edition, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="accessories",
    )

    # --- BGG identity/display (light — see docstring) ---
    bgg_id = models.PositiveIntegerField(null=True, blank=True, unique=True)
    bgg_name = models.CharField(max_length=300, blank=True)
    image_url = models.URLField(max_length=500, blank=True)
    bgg_rating = models.DecimalField(max_digits=5, decimal_places=3, null=True, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)

    url = models.URLField(max_length=500, blank=True, help_text="Store/reference link.")

    class Meta:
        verbose_name_plural = "accessories"
        ordering = ["name"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(game__isnull=True) | models.Q(edition__isnull=True)
                ),
                name="accessory_game_xor_edition",
            ),
        ]

    def __str__(self):
        return f"{self.brand} {self.name}".strip() if self.brand else self.name


class AccessoryCopy(models.Model):
    """An owned instance of an Accessory (DESIGN §16), mirroring Copy's
    relationship to Edition: ownership is per-user, keyed like
    SleeveInventory/Copy."""

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="accessory_copies",
    )
    accessory = models.ForeignKey(
        Accessory, on_delete=models.PROTECT, related_name="copies",
    )
    acquired_date = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        verbose_name_plural = "accessory copies"
        ordering = ["accessory__name"]

    def __str__(self):
        return f"{self.owner}'s copy of {self.accessory}"


# ===========================================================================
# §6  Purchases / crowdfunding
# ===========================================================================

class PledgeManager(models.Model):
    """A pledge-manager platform (DESIGN §6, issue #159/#181): admin-editable
    so a new PM or a URL fix doesn't need a deploy.
    """

    name = models.CharField(max_length=100, unique=True)
    default_url = models.URLField(
        max_length=500, blank=True,
        help_text="Shared login/dashboard URL used when a purchase has no pledge_manager_url of its own.",
    )

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Purchase(models.Model):
    """A campaign / order (DESIGN §6): one generalized system for board games
    and non-games, crowdfunding and plain preorders.

    Owned per-user, mirroring Copy ownership (visible to the group, attributed
    to the owner). Overall fulfillment is derived from the waves; the status
    field carries the §6 commitment lifecycle (watching -> placeholder ->
    committed -> passed/refunded/never-delivered).
    """

    class Platform(models.TextChoices):
        KICKSTARTER = "kickstarter", "Kickstarter"
        GAMEFOUND = "gamefound", "Gamefound"
        BACKERKIT = "backerkit", "BackerKit"
        OTHER = "other", "Other"

    class Status(models.TextChoices):
        # DESIGN §6 lifecycle. Wave fulfillment is tracked on the waves.
        WATCHING = "watching", "Watching"
        PLACEHOLDER = "placeholder", "Placeholder ($1 in)"
        COMMITTED = "committed", "Committed"
        # Backer-initiated exit (issue #34): decided not to go through with the
        # purchase — the $1 placeholder never upgraded, a watch let go, or a
        # committed pledge dropped. Distinct from REFUNDED (money came back
        # after committing) and NEVER_DELIVERED (creator-side failure).
        PASSED = "passed", "Passed"
        REFUNDED = "refunded", "Refunded"
        # Creator-side failure (issue #34): the creator ran out of money,
        # folded or vanished and the game never shipped; the committed money
        # is gone. Distinct from a backer who PASSED before committing.
        NEVER_DELIVERED = "never_delivered", "Never delivered"

    class PledgeManagerStatus(models.TextChoices):
        NOT_YET = "not_yet", "Not yet"
        SENT_OUT = "sent_out", "Sent out"
        FILLED_OUT = "filled_out", "Filled out"
        WONT_FILL_OUT = "wont_fill_out", "Won't fill out"
        NOT_NECESSARY = "not_necessary", "Not necessary"

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="purchases",
    )
    name = models.CharField(max_length=200, help_text='Campaign name, e.g. "Trickerion KS".')
    # §7 documents (digital deliverables, invoices) — generic relation, UI later.
    documents = GenericRelation("Document")
    platform = models.CharField(
        max_length=20, choices=Platform.choices, default=Platform.OTHER,
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.WATCHING,
    )
    campaign_url = models.URLField(max_length=1000, blank=True)
    # Drives the "ending soon" reminders for watched campaigns (DESIGN §11).
    # The Mastersheet doesn't carry it, so it starts blank for imported rows.
    campaign_end_date = models.DateField(null=True, blank=True)
    ordered_date = models.DateField(
        null=True, blank=True, help_text="When the pledge/preorder was placed.",
    )

    # Pledge-manager fields (DESIGN §6 / §11 "pledge managers closing soon").
    pledge_manager = models.ForeignKey(
        "PledgeManager", on_delete=models.PROTECT, null=True, blank=True,
        related_name="purchases",
    )
    pledge_manager_url = models.URLField(max_length=500, blank=True)
    pledge_manager_status = models.CharField(
        max_length=20, choices=PledgeManagerStatus.choices, blank=True,
    )
    pledge_manager_close_date = models.DateField(null=True, blank=True)

    # The sheet's excitement column mixes numbers and prose, so both survive.
    excitement = models.DecimalField(max_digits=3, decimal_places=1, null=True, blank=True)
    excitement_note = models.CharField(max_length=300, blank=True)
    comments = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["owner", "name"], name="unique_purchase_per_owner_name",
            ),
        ]

    def __str__(self):
        return self.name

    def get_pledge_manager_effective_url(self):
        """Purchase-specific link wins; else the PM's shared default (#159)."""
        if self.pledge_manager_url:
            return self.pledge_manager_url
        return self.pledge_manager.default_url if self.pledge_manager_id else ""

    @property
    def is_fulfilled(self):
        """DESIGN §6: overall status is derived from the waves — done when
        every wave reached a terminal state (arrived / never-arrived /
        cancelled)."""
        terminal = {Wave.Status.ARRIVED, Wave.Status.NEVER_ARRIVED, Wave.Status.CANCELLED}
        statuses = [wave.status for wave in self.waves.all()]
        return bool(statuses) and all(status in terminal for status in statuses)


class Wave(models.Model):
    """A shipment of a Purchase (DESIGN §6). Every Purchase gets a "Wave 1";
    more waves exist only when a campaign ships in parts. Address history
    comes from simple-history."""

    class DeliveryType(models.TextChoices):
        PHYSICAL = "physical", "Physical"
        DIGITAL = "digital", "Digital"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PRE_PRODUCTION = "pre_production", "Pre-production"
        PRODUCTION = "production", "Production"
        FULFILMENT = "fulfilment", "Fulfilment"
        ARRIVED = "arrived", "Arrived"
        # Terminal failures (DESIGN §6): publisher bankruptcy etc.
        NEVER_ARRIVED = "never_arrived", "Never arrived"
        CANCELLED = "cancelled", "Cancelled"

    TERMINAL_STATUSES = frozenset({"arrived", "never_arrived", "cancelled"})

    purchase = models.ForeignKey(
        Purchase, on_delete=models.CASCADE, related_name="waves",
    )
    number = models.PositiveIntegerField(default=1)
    # §7 documents (PnP files, wave-specific deliverables) — generic relation.
    documents = GenericRelation("Document")
    delivery_type = models.CharField(
        max_length=20, choices=DeliveryType.choices, default=DeliveryType.PHYSICAL,
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING,
    )

    # Dates: what was promised, what is currently expected, what happened.
    # Delay is derived, not stored (DESIGN §6).
    original_eta = models.DateField(null=True, blank=True)
    expected_arrival = models.DateField(null=True, blank=True)
    arrived_date = models.DateField(null=True, blank=True)

    # Digital waves carry no shipping/address/tracking (DESIGN §6); their
    # deliverables are file links, which land on Documents (§7) later.
    address = models.CharField(max_length=200, blank=True)
    tracking_url = models.URLField(max_length=1000, blank=True)

    history = HistoricalRecords()

    class Meta:
        ordering = ["purchase__name", "number"]
        constraints = [
            models.UniqueConstraint(
                fields=["purchase", "number"], name="unique_wave_number_per_purchase",
            ),
        ]

    def __str__(self):
        return f"{self.purchase} — Wave {self.number}"

    @property
    def delay_days(self):
        """Derived delay vs. the original ETA (DESIGN §6), or None."""
        reference = self.arrived_date or self.expected_arrival
        if self.original_eta and reference:
            return (reference - self.original_eta).days
        return None


class Product(models.Model):
    """A line item in a Wave (DESIGN §6): a game (FK to Game/Edition) or a
    non-game item. No independent status — it becomes a Copy on arrival or
    dies with a failed wave.

    Money is deferred (DESIGN §6): per-Product cost fields land here later,
    designed to never render a grand total.
    """

    class Kind(models.TextChoices):
        GAME = "game", "Board game"
        GAME_AND_EXPANSIONS = "game_and_expansions", "Game + expansions"
        EXPANSION = "expansion", "Expansion"
        PNP_GAME = "pnp_game", "Print-and-play game"
        GAMEBOOK = "gamebook", "Gamebook"
        ACCESSORY = "accessory", "Accessory"
        PROMO = "promo", "Promo"
        BOOK = "book", "Book"
        PUZZLE = "puzzle", "Puzzle"
        PLACEHOLDER_PLEDGE = "placeholder_pledge", "Placeholder pledge"
        OTHER = "other", "Other"

    # Kinds that represent playable games and therefore link to a Game record.
    GAME_KINDS = frozenset({"game", "game_and_expansions", "expansion", "pnp_game"})

    class TriState(models.TextChoices):
        YES = "yes", "Yes"
        NO = "no", "No"
        UNKNOWN = "unknown", "Unknown"

    wave = models.ForeignKey(Wave, on_delete=models.CASCADE, related_name="products")
    name = models.CharField(max_length=300)
    kind = models.CharField(max_length=30, choices=Kind.choices, default=Kind.OTHER)

    # Game products point at the Game (and later a concrete Edition) they
    # deliver; on arrival they convert into a Copy (DESIGN §6) — `copy` is
    # that conversion seam.
    game = models.ForeignKey(
        Game, on_delete=models.SET_NULL, null=True, blank=True, related_name="products",
    )
    edition = models.ForeignKey(
        Edition, on_delete=models.SET_NULL, null=True, blank=True, related_name="products",
    )
    copy = models.ForeignKey(
        Copy, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="source_products",
        help_text="The Copy this product became on arrival, if known.",
    )
    accessory_copy = models.ForeignKey(
        AccessoryCopy, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="source_products",
        help_text="The AccessoryCopy this product became on arrival, if known.",
    )

    # Reference link for items that are on BGG but are not modelled as Games
    # (gamebooks, accessories, promos).
    bgg_url = models.URLField(max_length=500, blank=True)
    # Digital deliverable / files link; becomes a Document when §7 lands.
    drive_url = models.URLField(max_length=1000, blank=True)

    # Provisional physical/sleeve attributes (DESIGN §6): what is known about
    # the contents before arrival. Concrete sleeve counts live in
    # ProductSleeveRequirement.
    contains_cards = models.CharField(max_length=10, choices=TriState.choices, blank=True)
    needs_sleeves = models.CharField(max_length=10, choices=TriState.choices, blank=True)
    fits_sleeved_note = models.CharField(
        max_length=100, blank=True, help_text='Sheet\'s "fits sleeved cards?" free text.',
    )
    miniatures_count = models.PositiveIntegerField(null=True, blank=True)
    insert_3d_note = models.CharField(
        max_length=300, blank=True,
        help_text="Provisional 3D-insert plan; moves to the Copy on arrival.",
    )
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["wave", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["wave", "name"], name="unique_product_name_per_wave",
            ),
        ]

    def __str__(self):
        return self.name


class ProductSleeveRequirement(models.Model):
    """CardSize -> count on a Product (DESIGN §5/§6): sleeve needs of a
    preordered item. The Edition-level SleeveRequirement can't hold these —
    preorders aren't Copies yet — so they live on the Product and feed the
    include-preorders shortfall toggle; on arrival/conversion they become
    Edition requirements."""

    product = models.ForeignKey(
        Product, on_delete=models.CASCADE, related_name="sleeve_requirements",
    )
    card_size = models.ForeignKey(
        CardSize, on_delete=models.PROTECT, related_name="product_requirements",
    )
    count = models.PositiveIntegerField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["product", "card_size"],
                name="unique_requirement_per_product_size",
            ),
        ]

    def __str__(self):
        return f"{self.product}: {self.count}× {self.card_size}"


# ===========================================================================
# §11  Notifications
# ===========================================================================

class ReminderLog(models.Model):
    """One sent §11 reminder email line-item, keyed by the deadline it was
    about. Makes the beat task idempotent: each (purchase, kind, deadline)
    emails exactly once, and a postponed deadline re-arms the reminder
    because the new date is a new key."""

    class Kind(models.TextChoices):
        PLEDGE_MANAGER = "pledge_manager", "Pledge manager closing"
        CAMPAIGN_END = "campaign_end", "Campaign ending"

    purchase = models.ForeignKey(
        Purchase, on_delete=models.CASCADE, related_name="reminder_logs",
    )
    kind = models.CharField(max_length=20, choices=Kind.choices)
    deadline = models.DateField()
    sent_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["purchase", "kind", "deadline"],
                name="unique_reminder_per_purchase_kind_deadline",
            ),
        ]

    def __str__(self):
        return f"{self.purchase}: {self.get_kind_display()} {self.deadline}"


class BggSyncDiff(models.Model):
    """One per-owner reconciliation diff from the BGG sync (DESIGN §8/§11):
    persisted so the dashboard can nag until reviewed. The sync upserts on
    (owner, category, bgg_id) — a dismissed row stays dismissed while the
    diff is still observed (§8: "not interested, never nag again") — and
    deletes rows no longer observed, so a diff that resolves and later
    reappears is a new, unreviewed occurrence. Never auto-acts: the rows
    are pure notification."""

    class Category(models.TextChoices):
        SUGGEST_ADD = "suggest_add", "On BGG, not in the app"
        MISSING_FROM_BGG = "missing_from_bgg", "In the app, missing from BGG"
        PREV_OWNED_ACTIVE = "prev_owned_active", "Active copy, previously owned on BGG"
        ARCHIVED_ON_BGG = "archived_on_bgg", "Archived, still owned on BGG"
        # New-expansion tracking (issue #64): one row per (owner, expansion)
        # once ExpansionSighting below has recorded a genuinely new sighting.
        NEW_EXPANSION = "new_expansion", "New expansion available"
        # Write-back (issue #117): a push_bgg_status attempt (or its enqueue)
        # failed. Cleared by a later successful push or a confirming read.
        PUSH_FAILED = "push_failed", "Status change not pushed to BGG"

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="sync_diffs",
    )
    category = models.CharField(max_length=20, choices=Category.choices)
    game = models.ForeignKey(
        Game, null=True, blank=True, on_delete=models.CASCADE, related_name="sync_diffs",
    )
    bgg_id = models.PositiveIntegerField()
    # Display name for game-less rows (suggest-adding); matches Game.name length.
    bgg_name = models.CharField(max_length=300, blank=True)
    note = models.TextField(blank=True)
    first_seen_at = models.DateTimeField(auto_now_add=True)
    # Set explicitly by the sync (not auto_now): bumping it on re-observation
    # must not imply any other field changed.
    last_seen_at = models.DateTimeField()
    dismissed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["category", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["owner", "category", "bgg_id"],
                name="unique_sync_diff_per_owner_category_bgg_id",
            ),
        ]

    def __str__(self):
        return (
            f"{self.owner}: {self.get_category_display()} — "
            f"{self.game or self.bgg_name} (BGG {self.bgg_id})"
        )


class ExpansionSighting(models.Model):
    """Global "first seen" fact for one (base game, expansion) pair (DESIGN
    §8, issue #64) — independent of who owns the base, so a second owner of
    an already-known base doesn't get "new" alerts for expansions the app
    already knew about. A base's first-ever sighting batch is its baseline
    (sync_new_expansions creates these rows without notifying); later
    batches that add a new row here are what actually trigger a per-owner
    BggSyncDiff (category=NEW_EXPANSION)."""

    base = models.ForeignKey(Game, on_delete=models.CASCADE, related_name="expansion_sightings")
    bgg_id = models.PositiveIntegerField()
    bgg_name = models.CharField(max_length=300, blank=True)
    # Set once a Game row exists for this expansion (e.g. via wishlist_add or
    # a later full sync); NULL until then.
    expansion = models.ForeignKey(
        Game, null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
    )
    first_seen_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["base", "bgg_id"], name="unique_expansion_sighting_per_base",
            ),
        ]

    def __str__(self):
        return f"{self.base}: expansion {self.bgg_name or self.bgg_id} (BGG {self.bgg_id})"


class PledgePlan(models.Model):
    """Pre-backing decision support for one Purchase (issue #186): compares
    candidate pledge bundles by cost and want-priority coverage, using only
    what the campaign itself offers. Kept off Purchase (DESIGN §6 keeps
    Purchase money-free) as its own required-FK child."""

    purchase = models.OneToOneField(
        Purchase, on_delete=models.CASCADE, related_name="pledge_plan",
    )
    currency = models.CharField(max_length=3, help_text='e.g. "EUR", "USD".')
    vat_rate = models.DecimalField(
        max_digits=5, decimal_places=2, null=True, blank=True,
        help_text=(
            "Import VAT % applied to each bundle's price+shipping, e.g. 21 "
            "for 21%. Leave blank if the campaign's prices already include "
            "VAT (the common case for EU-based campaigns)."
        ),
    )
    czk_rate = models.DecimalField(
        max_digits=8, decimal_places=4, null=True, blank=True,
        help_text="Manual currency->CZK rate, for display only.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Pledge plan: {self.purchase}"

    @property
    def priority_totals(self):
        """Count of this plan's items at each WishlistPriority level."""
        totals = {}
        for item in self.items.all():
            if item.want_priority is not None:
                totals[item.want_priority] = totals.get(item.want_priority, 0) + 1
        return totals


class PledgePlanItem(models.Model):
    class Category(models.TextChoices):
        BOARD_GAME = "board_game", "Board Game"
        EXPANSION = "expansion", "Expansion"
        PROMO = "promo", "Promo"
        ACCESSORY = "accessory", "Accessory"
        MERCH = "merch", "Merch"
        OTHER = "other", "Other"

    plan = models.ForeignKey(PledgePlan, on_delete=models.CASCADE, related_name="items")
    name = models.CharField(max_length=200)
    category = models.CharField(max_length=20, choices=Category.choices, default=Category.OTHER)
    want_priority = models.PositiveSmallIntegerField(
        choices=Game.WishlistPriority.choices, null=True, blank=True,
    )
    price = models.DecimalField(
        max_digits=8, decimal_places=2, null=True, blank=True,
        help_text=(
            "What this item would cost bought separately — informational "
            "only, used for the bundle's \"value\" comparison. Leave blank "
            "for items with no separate price (e.g. an exclusive add-on)."
        ),
    )
    notes = models.CharField(max_length=300, blank=True)
    exclusive = models.BooleanField(
        default=False,
        help_text="Only available through this campaign — won't be sold at retail or late pledge.",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["plan", "name"], name="unique_pledge_plan_item_name",
            ),
        ]

    def __str__(self):
        return self.name


class PledgePlanBundle(models.Model):
    plan = models.ForeignKey(PledgePlan, on_delete=models.CASCADE, related_name="bundles")
    name = models.CharField(max_length=200)
    items = models.ManyToManyField(PledgePlanItem, related_name="bundles", blank=True)
    price = models.DecimalField(
        max_digits=8, decimal_places=2,
        help_text="The actual advertised pledge cost for this bundle — usually less than the sum of its items' individual prices.",
    )
    shipping_cost = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    is_shortlisted = models.BooleanField(
        default=False,
        help_text="Starred as one of the candidates actually under consideration.",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["plan", "name"], name="unique_pledge_plan_bundle_name",
            ),
        ]

    def __str__(self):
        return self.name

    @property
    def value(self):
        """Sum of included items' individual prices — what you'd pay buying
        them separately, for comparison against the bundle's actual price.
        None if none of the included items have a known individual price."""
        priced = [item.price for item in self.items.all() if item.price is not None]
        return sum(priced, Decimal("0")) if priced else None

    @property
    def savings(self):
        value = self.value
        return value - self.price if value is not None else None

    @property
    def pre_vat_cost(self):
        return self.price + (self.shipping_cost or Decimal("0"))

    @property
    def vat_amount(self):
        rate = self.plan.vat_rate
        return self.pre_vat_cost * rate / Decimal("100") if rate is not None else Decimal("0")

    @property
    def total_cost(self):
        return self.pre_vat_cost + self.vat_amount

    @property
    def total_cost_czk(self):
        rate = self.plan.czk_rate
        return self.total_cost * rate if rate is not None else None

    @property
    def priority_coverage(self):
        """Per WishlistPriority level present on the plan: (included, total)."""
        plan_totals = self.plan.priority_totals
        included = {}
        for item in self.items.all():
            if item.want_priority is not None:
                included[item.want_priority] = included.get(item.want_priority, 0) + 1
        return {
            priority: (included.get(priority, 0), total)
            for priority, total in plan_totals.items()
        }


class WishlistEntry(models.Model):
    """A per-owner "I want this" entry (issue #64) — deliberately thin, no
    edition/archive/ready fields like Copy, since a wishlist item has no
    chosen edition or acquisition yet. Shaped so a follow-up issue can
    upsert into it from a real BGG wishlist sync; for now it's only written
    from the new-expansion widget's "Add to wishlist" action."""

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="wishlist_entries",
    )
    game = models.ForeignKey(Game, on_delete=models.CASCADE, related_name="wishlist_entries")
    priority = models.PositiveSmallIntegerField(choices=Game.WishlistPriority.choices)
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["priority", "-added_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["owner", "game"], name="unique_wishlist_entry_per_owner_game",
            ),
        ]

    def __str__(self):
        return f"{self.owner}: {self.game} ({self.get_priority_display()})"


class ToolRun(models.Model):
    """One background run of a maintenance tool (issue #90): the bulk BGG
    sync or the cover-image download, triggered from the superuser Tools page
    instead of the shell. Records status, timing and the captured command
    output so the page can show the last-run result, and doubles as the
    overlap guard — a `running` row of a kind blocks starting another of the
    same kind (see is_running). BGG is throttled and slow, so the actual work
    happens off-request in a Celery task (tasks.run_tool_command)."""

    class Kind(models.TextChoices):
        BGG_SYNC = "bgg_sync", "BGG sync"
        DOWNLOAD_COVERS = "download_covers", "Cover download"
        # Value kept short: the kind field is max_length=20, so the natural
        # "generate_cover_previews" (the command name) would overflow.
        GENERATE_PREVIEWS = "gen_previews", "Cover previews"

    class Status(models.TextChoices):
        RUNNING = "running", "Running"
        SUCCESS = "success", "Success"
        FAILED = "failed", "Failed"

    kind = models.CharField(max_length=20, choices=Kind.choices)
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.RUNNING,
    )
    triggered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="tool_runs",
    )
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    # Captured command stdout/stderr (or a traceback on failure).
    summary = models.TextField(blank=True)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self):
        return f"{self.get_kind_display()} ({self.get_status_display()}) {self.started_at:%Y-%m-%d %H:%M}"

    @classmethod
    def is_running(cls, kind):
        """True while a run of this kind is in flight — the overlap guard."""
        return cls.objects.filter(kind=kind, status=cls.Status.RUNNING).exists()

    @classmethod
    def latest(cls, kind):
        """The most recent run of this kind, or None — for last-run display."""
        return cls.objects.filter(kind=kind).first()


def sleeve_shortfall(owner, include_preorders=False):
    """Per-CardSize sleeve shortfall for one user (DESIGN §5) — logic, not
    schema.

    shortfall(size) = Σ(to-sleeve cards of that size across the owner's
    active, ready-to-play copies, per the editions' requirements) −
    (compatible sleeves in the owner's inventory). Not-yet-playable copies
    (issue #19 — e.g. an unprinted PnP copy) don't contribute; there's
    nothing to sleeve until it exists. Any product of the right size counts
    toward supply; the buy recommendation is rounded up to whole packs using
    the smallest pack_size sold for that size (default 100 when no product is
    known).

    include_preorders (the DESIGN §5 toggle) adds the sleeve needs of §6
    purchase Products on waves that are still under way. Arrived waves are
    excluded (an arrived game's needs belong to its Copy), as are terminal
    failures and passed/refunded/never-delivered or merely-watched purchases;
    placeholder ($1) pledges ARE counted — the Mastersheet counts them, and
    the sleeves will be needed if the pledge completes.

    Returns a list of dicts sorted by size:
      {card_size, to_sleeve, in_inventory, shortfall, packs_to_buy, games}
    `games` (issue #93) is that size's to_sleeve total broken down by
    contributor: {label, game_pk, to_sleeve}, sorted by descending count.
    game_pk is None for a preorder Product not yet linked to a Game — there's
    nothing to link to yet.
    """
    statuses = CopySleeveStatus.objects.filter(
        copy__owner=owner,
        copy__archive_status=Copy.ArchiveStatus.ACTIVE,
        status=CopySleeveStatus.Status.TO_SLEEVE,
    ).exclude(
        copy__ready_status=Copy.ReadyStatus.NOT_READY,
    ).select_related("copy__edition__game")

    requirements = {
        (r.edition_id, r.card_size_id): r.count
        for r in SleeveRequirement.objects.filter(
            edition__copies__owner=owner,
        )
    }
    needed = defaultdict(int)
    needed_by_game = defaultdict(lambda: defaultdict(int))
    game_labels = {}
    for status in statuses:
        count = requirements.get(
            (status.copy.edition_id, status.card_size_id), 0,
        )
        needed[status.card_size_id] += count
        game = status.copy.edition.game
        key = ("game", game.pk)
        game_labels[key] = (str(game), game.pk)
        needed_by_game[status.card_size_id][key] += count

    if include_preorders:
        preorder_needs = ProductSleeveRequirement.objects.filter(
            product__wave__purchase__owner=owner,
        ).exclude(
            product__wave__status__in=Wave.TERMINAL_STATUSES,
        ).exclude(
            product__wave__purchase__status__in=(
                Purchase.Status.WATCHING,
                Purchase.Status.PASSED,
                Purchase.Status.REFUNDED,
                Purchase.Status.NEVER_DELIVERED,
            ),
        ).select_related("product__game")
        for requirement in preorder_needs:
            needed[requirement.card_size_id] += requirement.count
            product = requirement.product
            if product.game_id:
                key = ("game", product.game_id)
                game_labels[key] = (str(product.game), product.game_id)
            else:
                key = ("product", product.pk)
                game_labels[key] = (product.name, None)
            needed_by_game[requirement.card_size_id][key] += requirement.count

    supply = defaultdict(int)
    pack_sizes = defaultdict(list)
    for inventory in SleeveInventory.objects.filter(owner=owner).select_related("product"):
        supply[inventory.product.card_size_id] += inventory.total_sleeves
        pack_sizes[inventory.product.card_size_id].append(inventory.product.pack_size)
    for product in SleeveProduct.objects.filter(card_size_id__in=needed):
        pack_sizes[product.card_size_id].append(product.pack_size)

    results = []
    for size in CardSize.objects.filter(pk__in=needed).order_by("width_mm", "height_mm"):
        shortfall = max(0, needed[size.pk] - supply[size.pk])
        pack_size = min(pack_sizes[size.pk], default=100)
        games = sorted(
            (
                {"label": game_labels[key][0], "game_pk": game_labels[key][1], "to_sleeve": count}
                for key, count in needed_by_game[size.pk].items()
            ),
            key=lambda g: (-g["to_sleeve"], g["label"]),
        )
        results.append({
            "card_size": size,
            "to_sleeve": needed[size.pk],
            "in_inventory": supply[size.pk],
            "shortfall": shortfall,
            "packs_to_buy": math.ceil(shortfall / pack_size),
            "games": games,
        })
    return results


# ===========================================================================
# §7  Files / documents
# ===========================================================================

# Reserved on Windows and/or POSIX. We strip only these (and control chars),
# keeping spaces and unicode so the on-disk tree stays as human-readable as the
# Google-Drive folders it replaces.
_UNSAFE_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _sanitize_name(name):
    """Filesystem-safe but still human-readable: drop reserved characters,
    collapse whitespace, trim trailing dots/spaces (Windows hates those).
    Falls back to a placeholder when nothing usable is left."""
    cleaned = _UNSAFE_NAME_CHARS.sub("", name or "")
    cleaned = " ".join(cleaned.split()).strip(" .")
    return cleaned or "unnamed"


def _document_host_folder(host):
    """The media subfolder for a document's host (DESIGN §7). Each level is
    '<name> [<pk>]' so the tree sorts alphabetically by name yet stays findable
    by the stable id after a rename, and two same-named hosts never collide.

    An expansion's documents nest under its base game's folder (issue #99), so a
    game's whole set — base + expansions + their editions — sits together:

        games/<base> [bpk]/                              base / standalone game
        games/<base> [bpk]/<edition> [edpk]/             a base game's edition
        games/<base> [bpk]/<expansion> [epk]/            an expansion
        games/<base> [bpk]/<expansion> [epk]/<ed> [edpk] an expansion's edition

    This naming is the contract the discover_documents command parses back."""
    def seg(name, pk):
        return f"{_sanitize_name(name)} [{pk}]"

    def game_path(game):
        # Re-root an expansion under its primary base; base games stay at top.
        base = game.primary_base
        if base is not None:
            return f"games/{seg(base.name, base.pk)}/{seg(game.name, game.pk)}"
        return f"games/{seg(game.name, game.pk)}"

    if isinstance(host, Game):
        return game_path(host)
    if isinstance(host, Edition):
        return f"{game_path(host.game)}/{seg(host.name or 'default', host.pk)}"
    if isinstance(host, Series):
        return f"series/{seg(host.name, host.pk)}"
    if isinstance(host, Purchase):
        return f"purchases/{seg(host.name, host.pk)}"
    if isinstance(host, Wave):
        return (f"purchases/{seg(host.purchase.name, host.purchase_id)}/"
                f"wave-{host.number}")
    # Unknown host: park it so an upload is never lost to a raised exception.
    return f"other/{host._meta.model_name}-{host.pk}"


class DocumentStorage(FileSystemStorage):
    """Storage for §7 document uploads, with two departures from the default:

    - it keeps the human-readable filename document_upload_path already built
      (the default `get_valid_name` would rewrite spaces to underscores), since
      the point of §7 is a folder tree as browsable as the Google Drive it
      replaces; the path segments are already sanitized upstream.
    - `location` reads MEDIA_ROOT dynamically rather than caching it at init,
      so tests' override_settings(MEDIA_ROOT=...) is honoured.
    """

    def get_valid_name(self, name):
        return name

    @property
    def base_location(self):
        return self._value_or_setting(self._location, settings.MEDIA_ROOT)

    @property
    def location(self):
        return os.path.abspath(self.base_location)


def document_upload_path(instance, filename):
    """Where an uploaded Document file lands (DESIGN §7): a human-readable tree
    under media/documents/, e.g. 'documents/games/Wingspan [42]/Rulebook.pdf'.
    The label names the file (falling back to the upload's own stem); the
    original extension is preserved. content_object is set before .save(), so
    the host is available here."""
    original = PurePosixPath(filename)
    stem = instance.label or original.stem
    name = f"{_sanitize_name(stem)}{original.suffix.lower()}"
    folder = _document_host_folder(instance.content_object)
    return f"documents/{folder}/{name}"


class Document(models.Model):
    """A file and/or external link attached to a game, edition, purchase or
    wave (DESIGN §7): rulebooks, PnP files, reference sheets, insert plans.

    Both an external_url and an uploaded file may coexist on one record (the
    publisher's official link plus the copy you downloaded). Attaches through a
    generic relation so one model serves every §7 host; only the Game UI is
    wired so far. Replaces the old Google-Drive-folders workflow."""

    class Type(models.TextChoices):
        RULEBOOK = "rulebook", "Rulebook"
        PNP = "pnp", "PnP file"
        REFERENCE = "reference", "Reference sheet"
        INSERT_PLAN = "insert_plan", "Insert plan"
        OTHER = "other", "Other"

    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.PositiveIntegerField()
    content_object = GenericForeignKey("content_type", "object_id")

    doc_type = models.CharField(
        max_length=20, choices=Type.choices, default=Type.OTHER,
    )
    label = models.CharField(max_length=200, blank=True)
    external_url = models.URLField(max_length=1000, blank=True)
    file = models.FileField(
        upload_to=document_upload_path, storage=DocumentStorage, blank=True,
    )
    is_primary = models.BooleanField(
        default=False,
        help_text="Pin above the rest. More than one document may be primary.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["content_type", "object_id"])]
        # Pinned first, then by type, then label — the order the UI lists them.
        ordering = ["-is_primary", "doc_type", "label"]

    def __str__(self):
        return self.label or self.get_doc_type_display()

    def clean(self):
        # §7 invariant: a document is a link and/or a file — never neither.
        if not self.external_url and not self.file:
            raise ValidationError(
                "A document needs an external URL, an uploaded file, or both.")

    @property
    def display_url(self):
        """The href for this document: the uploaded file if present, else the
        external link (mirrors ExternalLink.resolved_url)."""
        if self.file:
            return self.file.url
        return self.external_url
