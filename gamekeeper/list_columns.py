"""Collection list-view column registry (issue #54).

Every column the list view can show lives in LIST_COLUMNS, in the order the
Settings page offers them. A user picks and orders their own subset
(Membership.collection_columns); an empty selection means
DEFAULT_LIST_COLUMNS, which is the fixed column set the list shipped with
(issue #92). "name" is always the first column and can't be removed.

Each column's ``cell(row)`` returns ``(sort_key, text)``. A None sort_key
means "no value": the row sorts last in either direction and the text falls
back to "—". Keeping sort key and text together means each value is computed
once per row, and an enum column can sort by its scale (e.g. language
dependency No text → Difficult) while displaying its label.

owner_only columns read the owner's personal state (copies, lending, plays)
and are never offered on the shared/public views (DESIGN §3).
"""

from dataclasses import dataclass, field
from functools import cached_property
from typing import Callable

from .models import Copy, CopySleeveStatus, Edition, Game, Loan, Tag

MISSING = "—"
# Sort ordinal for per-copy values that disagree ("mixed"/"multiple"): after
# every uniform value, before rows with no value at all.
_MIXED_ORDINAL = 10_000


def active_copies(game):
    """A game's non-archived copies across all its editions. Prefetch
    "editions__copies" (or deeper) to avoid N+1."""
    return [
        copy
        for edition in game.editions.all()
        for copy in edition.copies.all()
        if copy.archive_status == Copy.ArchiveStatus.ACTIVE
    ]


def copy_summary(game):
    """(count, location_label, keep_label) over a game's active copies
    (issue #92 list view). Location/keep-status show the shared value when
    every active copy agrees, else "multiple"/"mixed" — a single row can't
    show one game's several copies individually."""
    copies = active_copies(game)
    if not copies:
        return 0, MISSING, MISSING

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
        keep_label = copies[0].get_keep_status_display() or MISSING

    return len(copies), location_label, keep_label


class Row:
    """Per-row inputs for the cell functions: the tile's representative game
    plus lazily computed copy-level data, so a column nobody selected costs
    nothing."""

    def __init__(self, tile, extras):
        self.tile = tile
        self.game = tile["stat_game"]
        self.extras = extras

    @cached_property
    def copies(self):
        return active_copies(self.game)

    @cached_property
    def active_loans(self):
        return [loan for copy in self.copies for loan in copy.loans.all()
                if loan.returned_at is None]


@dataclass(frozen=True)
class ListColumn:
    key: str
    label: str
    cell: Callable[[Row], tuple]
    owner_only: bool = False
    group: str = "Game"
    # Extra prefetch_related lookups this column needs on the Game queryset.
    prefetch: tuple = field(default=())


# --- cell helpers ----------------------------------------------------------

def _plain(value, text=None):
    """A value that sorts and displays as itself; None → missing."""
    if value is None or value == "":
        return None, MISSING
    return value, str(value) if text is None else text


def _choice(value, choices):
    """An enum value sorted by its position in the choices scale."""
    if not value:
        return None, MISSING
    order = [v for v, _ in choices]
    label = dict(choices).get(value, value)
    return (order.index(value) if value in order else len(order)), label


def _names(items):
    """A comma-joined, alphabetical name list sorted by its first entry."""
    names = sorted(str(item) for item in items)
    if not names:
        return None, MISSING
    return names[0].casefold(), ", ".join(names)


def _uniform_choice(values, choices):
    """A per-copy enum: the shared value when every copy agrees, else
    "mixed" (sorted after every uniform value)."""
    distinct = set(values)
    if not distinct:
        return None, MISSING
    if len(distinct) > 1:
        return _MIXED_ORDINAL, "mixed"
    return _choice(distinct.pop(), choices)


def _date(value):
    if value is None:
        return None, MISSING
    return value, value.isoformat()


# --- game-level cells ------------------------------------------------------

def _players(row):
    low, high = row.game.effective_player_range()
    if not (low or high):
        return None, MISSING
    return low, f"{low or '?'}–{high or '?'}"


def _playtime(row):
    low, high = row.game.min_playtime, row.game.max_playtime
    if not (low or high):
        return None, MISSING
    text = str(low or high)
    if low and high and low != high:
        text += f"–{high}"
    return low or high, f"{text} min"


def _weight(row):
    weight = row.game.weight
    return (None, MISSING) if weight is None else (weight, f"{weight} / 5")


def _rating(row):
    rating = row.game.bgg_rating
    return (None, MISSING) if rating is None else (rating, f"{rating:.2f}")


def _plays(row):
    # bgg_numplays: NULL means unsynced or zero — same signal as the
    # "played" filter (issue #63), so 0 shows as "—" too.
    return _plain(row.game.bgg_numplays or None)


def _owned_expansions(row):
    count = sum(1 for expansion in row.game.expansions.all() if expansion.is_owned)
    return _plain(count or None)


def _tags(kind):
    def cell(row):
        return _names(gt.tag.name for gt in row.game.game_tags.all() if gt.tag.kind == kind)
    return cell


def _series(row):
    series = row.game.series
    return (None, MISSING) if series is None else (series.name.casefold(), series.name)


def _wishlist(row):
    priority = row.game.bgg_wishlist_priority
    if priority is None:
        return None, MISSING
    return priority, Game.WishlistPriority(priority).label


# --- owner-only, copy-level cells -----------------------------------------

def _last_played(row):
    return _date(row.extras.get("last_played", {}).get(row.game.pk))


def _copy_count(row):
    return _plain(row.tile["copy_count"] or None)


def _location(row):
    label = row.tile["location_label"]
    return (None, MISSING) if label == MISSING else (label.casefold(), label)


def _keep(row):
    # Blank keep_status counts as a value here, so a blank + "Keep" pair
    # reads "mixed" — matching copy_summary's keep_label.
    return _uniform_choice([c.keep_status for c in row.copies], Copy.KeepStatus.choices)


# Sleeve summary scale, best to worst (issue #54).
_SLEEVE_LABELS = ["Sleeved", "Partial", "To sleeve", "Not sleeved"]


def _sleeves(row):
    statuses = [s.status for copy in row.copies for s in copy.sleeve_statuses.all()]
    if not statuses:
        return None, MISSING
    sleeved = CopySleeveStatus.Status.SLEEVED
    if all(s == sleeved for s in statuses):
        label = "Sleeved"
    elif sleeved in statuses:
        label = "Partial"
    elif CopySleeveStatus.Status.TO_SLEEVE in statuses:
        label = "To sleeve"
    else:
        label = "Not sleeved"
    return _SLEEVE_LABELS.index(label), label


def _sleeve_cards(row):
    editions = {copy.edition_id: copy.edition for copy in row.copies}
    total = sum(req.count for edition in editions.values()
                for req in edition.sleeve_requirements.all())
    return _plain(total or None)


def _added(row):
    dates = [copy.acquired_date or copy.created_at.date() for copy in row.copies]
    return _date(min(dates) if dates else None)


def _excitement(row):
    values = [c.excitement for c in row.copies if c.excitement is not None]
    return _plain(max(values) if values else None)


def _immune(row):
    if not row.copies:
        return None, MISSING
    immune = any(c.immune for c in row.copies)
    return (0, "Immune") if immune else (1, "No")


def _ready(row):
    if not row.copies:
        return None, MISSING
    if all(c.ready_status == Copy.ReadyStatus.READY for c in row.copies):
        return 0, "Ready"
    return 1, "Not ready"


def _edition(row):
    return _names({c.edition.name or "Default" for c in row.copies})


def _edition_choice(attr, choices):
    def cell(row):
        return _uniform_choice(
            [getattr(c.edition, attr) for c in row.copies if getattr(c.edition, attr)], choices)
    return cell


def _loan(row):
    loans = row.active_loans
    if not loans:
        return None, MISSING
    if len(loans) > 1:
        return (1, ""), f"{len(loans)} loans"
    loan = loans[0]
    preposition = "to" if loan.direction == Loan.Direction.LENT_OUT else "from"
    verb = "Lent" if loan.direction == Loan.Direction.LENT_OUT else "Borrowed"
    text = f"{verb} {preposition} {loan.counterparty}"
    return (0, text.casefold()), text


def _loan_due(row):
    dates = [loan.expected_return_date for loan in row.active_loans if loan.expected_return_date]
    return _date(min(dates) if dates else None)


def _upgrade(attr):
    def cell(row):
        values = [getattr(c, attr) for c in row.copies
                  if getattr(c, attr) != Copy.UpgradeStatus.NONE]
        return _uniform_choice(values, Copy.UpgradeStatus.choices)
    return cell


# --- registry --------------------------------------------------------------

LIST_COLUMNS = [
    # The name cell is rendered specially by game_list.html (links, series
    # badge); only its sort key comes from here.
    ListColumn("name", "Name", lambda row: (row.tile["sort_key"], "")),
    ListColumn("type", "Type", lambda row: _plain(row.game.get_type_display())),
    ListColumn("year", "Year", lambda row: _plain(row.game.year_published)),
    ListColumn("players", "Players", _players),
    ListColumn("playtime", "Playtime", _playtime),
    ListColumn("weight", "Weight", _weight),
    ListColumn("rank", "BGG rank", lambda row: _plain(row.game.bgg_rank)),
    ListColumn("rating", "BGG rating", _rating),
    ListColumn("plays", "Plays", _plays),
    ListColumn("designers", "Designers", lambda row: _names(row.game.designers.all()),
               prefetch=("designers",)),
    ListColumn("editions", "Editions", lambda row: _plain(len(row.game.editions.all()))),
    ListColumn("expansions", "Owned expansions", _owned_expansions),
    ListColumn("series", "Series", _series),
    ListColumn("families", "Families", lambda row: _names(row.game.families.all())),
    ListColumn("mechanics", "Mechanics", _tags(Tag.Kind.MECHANIC),
               prefetch=("game_tags__tag",)),
    ListColumn("themes", "Themes", _tags(Tag.Kind.THEME), prefetch=("game_tags__tag",)),
    ListColumn("language", "Language dependency",
               lambda row: _choice(row.game.language_dependency, Game.LanguageDependency.choices)),
    ListColumn("app", "Companion app",
               lambda row: _choice(row.game.companion_app, Game.AppUse.choices)),
    ListColumn("bgg_status", "BGG status",
               lambda row: _choice(row.game.bgg_collection_status, Game.BggCollectionStatus.choices)),
    ListColumn("wishlist", "Wishlist priority", _wishlist),

    ListColumn("last_played", "Last played", _last_played, owner_only=True, group="Plays"),

    ListColumn("location", "Location", _location, owner_only=True, group="Copies"),
    ListColumn("keep", "Keep/leave", _keep, owner_only=True, group="Copies"),
    ListColumn("copies", "Copies", _copy_count, owner_only=True, group="Copies"),
    ListColumn("added", "Added", _added, owner_only=True, group="Copies"),
    ListColumn("excitement", "Excitement", _excitement, owner_only=True, group="Copies"),
    ListColumn("immune", "Immune", _immune, owner_only=True, group="Copies"),
    ListColumn("ready", "Ready", _ready, owner_only=True, group="Copies"),
    ListColumn("edition", "Edition", _edition, owner_only=True, group="Copies"),
    ListColumn("components_language", "Components language",
               _edition_choice("components_language", Edition.ComponentsLanguage.choices),
               owner_only=True, group="Copies"),
    ListColumn("box_size", "Box size",
               _edition_choice("size_category", Edition.SizeCategory.choices),
               owner_only=True, group="Copies"),

    ListColumn("loan", "Loan", _loan, owner_only=True, group="Lending",
               prefetch=("editions__copies__loans__counterparty_user",)),
    ListColumn("loan_due", "Loan due back", _loan_due, owner_only=True, group="Lending"),

    ListColumn("sleeves", "Sleeves", _sleeves, owner_only=True, group="Upgrades",
               prefetch=("editions__copies__sleeve_statuses",)),
    ListColumn("sleeve_cards", "Cards to sleeve", _sleeve_cards, owner_only=True,
               group="Upgrades", prefetch=("editions__sleeve_requirements",)),
    ListColumn("insert_3d", "3D insert", _upgrade("insert_3d"), owner_only=True, group="Upgrades"),
    ListColumn("card_dividers", "Card dividers", _upgrade("card_dividers"),
               owner_only=True, group="Upgrades"),
    ListColumn("accessories_3d", "3D accessories", _upgrade("accessories_3d"),
               owner_only=True, group="Upgrades"),
    ListColumn("other_accessories", "Other accessories", _upgrade("other_accessories"),
               owner_only=True, group="Upgrades"),
]
COLUMNS_BY_KEY = {column.key: column for column in LIST_COLUMNS}

DEFAULT_LIST_COLUMNS = [
    "name", "type", "year", "players", "playtime", "weight", "rank",
    "location", "keep", "copies",
]


def clean_column_keys(keys):
    """Known, de-duplicated keys in the given order, minus the fixed "name"
    column — what gets stored on Membership.collection_columns."""
    seen, cleaned = set(), []
    for key in keys:
        if key in COLUMNS_BY_KEY and key != "name" and key not in seen:
            seen.add(key)
            cleaned.append(key)
    return cleaned


def resolve_columns(keys, *, owner_view):
    """The ListColumns to render for a stored selection: the defaults when
    empty, unknown keys dropped, owner-only ones dropped off the owner's own
    view, and "name" always first."""
    keys = clean_column_keys(keys or DEFAULT_LIST_COLUMNS)
    columns = [COLUMNS_BY_KEY["name"]]
    columns += [COLUMNS_BY_KEY[key] for key in keys
                if owner_view or not COLUMNS_BY_KEY[key].owner_only]
    return columns


def settings_rows(keys):
    """(column, selected) for the Settings page: the selected columns first
    in their saved order, then the rest in registry order."""
    selected = clean_column_keys(keys or DEFAULT_LIST_COLUMNS)
    rest = [c.key for c in LIST_COLUMNS if c.key != "name" and c.key not in selected]
    return ([(COLUMNS_BY_KEY[k], True) for k in selected]
            + [(COLUMNS_BY_KEY[k], False) for k in rest])


def column_prefetches(columns):
    return sorted({lookup for column in columns for lookup in column.prefetch})


def annotate_row(tile, columns, extras):
    """Attach list-view row data to a tile in place: the stat game (a series
    row reports its primary_game's stats, the same representative the grid
    uses for its cover), the copy summary, each column's sort key and the
    display cells (every column but the specially rendered name)."""
    game = tile["series"].primary_game if "series" in tile else tile["game"]
    tile["stat_game"] = game
    tile["players_low"], tile["players_high"] = game.effective_player_range()
    tile["copy_count"], tile["location_label"], tile["keep_label"] = copy_summary(game)
    row = Row(tile, extras)
    tile["sort_values"], tile["cells"] = {}, []
    for column in columns:
        sort_key, text = column.cell(row)
        tile["sort_values"][column.key] = sort_key
        if column.key != "name":
            tile["cells"].append(text)


def sort_tiles(tiles, sort, columns):
    """Reorder annotated tiles by a "col" / "-col" sort spec. Unknown or
    unselected columns fall back to name. Tiles missing the sorted value sort
    last regardless of direction — pushing them to the top on a descending
    sort would bury every game that actually has data."""
    column = sort[1:] if sort.startswith("-") else sort
    if column not in {c.key for c in columns}:
        column, sort = "name", "name"
    descending = sort.startswith("-")
    present = [t for t in tiles if t["sort_values"][column] is not None]
    missing = [t for t in tiles if t["sort_values"][column] is None]
    present.sort(key=lambda t: t["sort_values"][column], reverse=descending)
    return present + missing


def sort_column_context(sort, columns):
    """Per-column header state for game_list.html: the label, the sort spec
    a click on this header should request next (toggling asc/desc on the
    active column, defaulting to ascending on any other), and the arrow to
    show now."""
    active_column = sort[1:] if sort.startswith("-") else sort
    active_descending = sort.startswith("-")
    context = []
    for column in columns:
        active = column.key == active_column
        is_descending = active and active_descending
        context.append({
            "key": column.key,
            "label": column.label,
            "next_sort": column.key if is_descending else (f"-{column.key}" if active else column.key),
            "direction": "desc" if is_descending else ("asc" if active else None),
        })
    return context

