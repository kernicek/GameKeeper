# GameKeeper — Design

A self-hostable, multi-user **board-game collection manager** that replaces a large personal
tracking spreadsheet ("Boardgame Mastersheet"). This document is the agreed design from a
requirements-gathering session; it is the source of truth for the initial build.

---

## 1. Product summary

Core jobs the app must do well:

1. **Organize the collection** — browse, sort, filter, rich detail pages (cover-art forward).
2. **Plan card sleeving** — bulk sleeve inventory, know what you have vs. need per card size.
3. **Choose what to play** — the "GameChooser": filter by players / time / type / mechanic / etc.
4. **Track crowdfunding & purchases** — Kickstarter/Gamefound/etc., for board games **and non-games**.
5. **Support decisions** — what to cull, what to back before a campaign ends, which new expansions exist.

**Explicitly not** a play-logger. Plays are tracked in **BG Stats** (which syncs to BGG); the app only
**reads and displays** plays from BGG. No in-app play logging, no per-game play schema.

---

## 2. Architecture & stack

Mirror the proven stack from a separate work project for **infrastructure**, but give this app a
**completely distinct visual identity** (see §14) — it must not feel like the work app at a glance.

| Concern | Choice |
|---|---|
| Framework | Django 5 |
| Database | **SQLite** (WAL mode, `busy_timeout`) — see §2.1 |
| Cache / broker | Redis |
| Background jobs | Celery worker + beat |
| App server | Gunicorn |
| Reverse proxy | Nginx (TLS) |
| Packaging | Docker Compose; self-hosted on Synology/TrueNAS; host volumes mounted at `/data/*` |
| History/audit | django-simple-history |
| Frontend | Server-rendered **Bootstrap 5.3** templates + **htmx** (no SPA) |
| Media | Local filesystem volume (`/data/media`); optional S3 later |
| Email | SMTP (Purelymail) via Django email backend, sent from Celery beat |
| Push notifications | ntfy (self-hosted), per-user topic, sent from Celery beat |

### 2.1 Why SQLite (not Postgres)

Scale is a couple of users per instance — trivial write volume, read-dominated. The only background
writer is **BGG sync**, which is hard rate-limited by BGG (throttled + 202-retry), so it runs at
**`concurrency=1`** and is effectively serial. This sidesteps the concurrent-writer lock contention
that forces larger apps onto Postgres.

Mitigations baked in:
- `PRAGMA journal_mode=WAL` — concurrent readers don't block the writer.
- `busy_timeout` (~5s) in the DB `OPTIONS` — writers wait for a lock instead of erroring.
- Sync worker pinned to `concurrency=1`.

**Postgres escape hatch:** if the instance ever grows to many concurrent users or heavy parallel
background writes, switch via the ORM and add a `postgres` service to compose — no app-code changes.
Backups meanwhile are "copy the `.sqlite3` file."

---

## 3. Users, groups & visibility

- **Every user belongs to exactly one Group** (an auto-created "group of one" on signup; can later join a
  household). *Group* is the universal collection/sharing boundary.
- **Copy ownership is per-user.** Personal/physical attributes (sleeving, painting, keep-status,
  excitement, location) live on the individual's Copy — visible to the group but attributed to the owner.
- A Group **unions** its members' copies into one shared collection view.

### Roles
- **Group owner/admin** — invite members, manage shares & visibility.
- **Group member** — owns copies.
- **Viewer** — external, read-only via a share grant.
- **Superuser** (global) — instance admin: BGG token, users, settings.

### Visibility tiers (per group collection)
1. **Private** — group members only.
2. **Shared** — explicit read-only grants to specific users **and/or** other groups (`ShareGrant`
   targeting a user or a group).
3. **Server-public** — any **logged-in** user on the instance.
4. **Anonymous share link** — an **unguessable token** URL for non-users. Restricted projection:
   **collection only, no preorders**, and a reduced, curated safe field set (see below).

**Anonymous safe projection** (curated in code, not user-editable in v1):
show cover image, title, BGG link, player count, playtime, weight/rating, categories/mechanisms,
owning group name. **Hide** price/purchase info, storage location, keep-status, excitement, personal
notes, and anything from Preorders / Sleeves / Minis.

**Per-location share link** (issue #123): a `Location` can carry its own unguessable `share_token`,
mirroring the tier-4 group link but pinned to that one `Location` — the visitor sees only games with
an active Copy there. Same safe projection and no-login gate as tier 4; the group's full location
list is never exposed, so this is additive to the "hide storage location" rule above, not a
relaxation of it.

---

## 4. Core data model

Three-layer spine: **Game → Edition → Copy**.

### Game (a BGG "thing" / a title)
- Primary **BggLink** (stores only the BGG **id**; URL derived from a one-place template).
- Multiple BggLinks allowed, exactly **one primary**. Primary drives synced stats/image.
- `type`: base game | expansion.
- Synced from BGG: name, year, image/thumbnail, min/max players, playtime, weight, rank, rating,
  mechanics, categories, expansion relationships.
- Curated (not from BGG): themes, game-type, campaign-structure, language fields, etc. (see §10).
- **ExternalLink** (generic): `(type, url, label)`. Each link *type* optionally has a URL template —
  if present, store an id and derive the URL (BGG-style); if not (e.g. **zatrolené-hry.cz** ugly
  slugs), store the full pasted URL.

### Edition (a BGG "version": base / Collector's / Anniversary)
- Lightweight — a default auto-edition exists when you don't care about editions; Copy always points at one.
- Physical attributes live here (they differ per edition): box dimensions, size category, **sleeve
  requirements** (`CardSize → count`), optional BGG version id.

### Copy (a user owns a specific Edition)
- Owner (User), Edition, acquired date, **upgrade history** (e.g. owned Collector's → upgraded to Anniversary).
- Personal: **excitement**, keep/curation fields (§11), upgrades/customizations (3D-printed insert,
  card dividers, 3D-printed accessories/other), **current location** (§9), sleeved status (§5).
- **Archive**: `active | archived(reason ∈ {sold, gifted, lost, culled}, date)` — archived copies are
  retained for reference, hidden from active views, still findable.
- **Minis seam** (deferred module): a Copy can gain minis later without a schema rewrite.

### Relations & expansions
- Expansion → base(s) via M2M (an expansion can expand several bases; a base has many expansions).
- Expansions carry **optional stat overrides** (min/max players, playtime delta, …).
- **Effective stats are per-Copy / per-user**: a base Copy's effective range = base stats combined with
  overrides from expansions **the same user owns**. **No cross-user pooling** — another member's
  expansion never modifies your copy.
- Expansion-modified stats are **highlighted on the game page** ("2–6 with *Passion*").
- **Filtering uses effective values** (with a "will fit / could fit" toggle for playtime).
- Group-level "what can we play at N?": a game qualifies if **at least one member individually** owns a
  base+expansion combo reaching N.

### Series & families (2026-07 decision, issue #21)

Two distinct grouping entities — deliberately **not** one concept:

- **Series** — a group of **interchangeable** games: (near-)identical rules, stats and duration
  ("the MicroMacro series"; also Timeline, Beasty Bar, Dale of Merchants — some even share one
  physical box). Single-membership FK on Game. The collection grid **collapses a series to one
  tile** (primary member's cover, custom override possible); expanding offers the series link
  plus each member. A Series has its own **detail page sharing the game page's template base** —
  the same Purchases/Copies/Documents sections fed a union over members, stats from the primary
  member, plays summed with a per-member breakdown. The GameChooser treats a series as one row.
  A series is a **browse/display layer only**: Copies, Editions and sleeve requirements stay
  per-member and are never merged (Dale 2's cards ≠ Dale 1's).
- **Family** — a loose association of **distinct but related** games in one line ("the Burgle
  Bros family": same designer/world, genuinely different gameplay). M2M — a game may sit in
  several families. **Never collapses the grid**; surfaces as a family badge on
  member detail pages and as a GameChooser filter facet.

The names align with BGG: **BGG's "family" taxonomy corresponds to our Family**, so the future
`bgg_family_id` seed field lives there. BGG family links ride on `/thing` (token-gated, §15) and
are mostly junk rows; only `Game:`/`Series:` types are worth seeding. **Manual curation is primary
for both entities.**

---

## 5. Sleeves

Two distinct models:

- **CardSize** — a card dimension (W×H mm) with name/aliases (`63×88` = "Standard"). What a game's cards
  *require*. Seeded from the user's list; extensible.
- **SleeveProduct** — a real purchasable product: brand (Tlama / AT / Gamegenic / custom), **fits a
  CardSize**, finish (matte/glossy), back (clear/colored/printed), **`pack_size`** (default 100,
  overridable). All product attributes live here.

Mechanics:
- **Requirements on Edition**: `CardSize → count` (Collector's ≠ base).
- **Inventory in packs** per SleeveProduct (+ optional loose leftovers from opened packs).
- **Sleeved status per Copy, per CardSize**: `not-sleeved | to-sleeve | sleeved`, optionally recording
  **which SleeveProduct** was used (nullable) — reproduces the sheet's per-brand breakdown.
- **Shortfall** computed at **CardSize level**: `Σ(to-sleeve cards of size X) − (compatible sleeves in
  inventory)`, **rounded up to whole packs**. Any product of the right size counts toward supply.
  Toggle to include/exclude preorder needs.

**BGG card-size pre-fill — investigated, not feasible (issue #131, closed 2026-07-06).** There is no
automatic BGG source for card sizes. Probed live with the registered-app token: `thing?versions=1` returns
only physical **box** dimensions and version metadata (no card data); the geekitems JSON carries no
sleeve/card-size/dimension fields; the HTML game page is `403` (scraping blocked, and it's a React SPA fed
by those same dataless JSON endpoints). BGG has no card-size field in its schema, so both the API pull and
a page scraper are dead ends. Consequences:
- Sleeve requirements are **manual-entry only** — the in-app editor (issue #129) and the `import_sleeves`
  Mastersheet import (curated, bgg_id-keyed) are the sole sources.
- Reopen only if BGG ever exposes structured card data.

---

## 6. Purchases / crowdfunding

Hierarchy: **Purchase → Wave → Product**. One generalized system for board games **and** non-games,
crowdfunding **and** plain preorders.

### Purchase (campaign)
- platform, pledge-manager fields, excitement, comments, links, **`campaign_end_date`**.
- **Lifecycle:** `watching → placeholder ($1 in) → committed → [wave fulfillment] → abandoned/refunded`.
  - **watching** = interested, campaign live, not yet backed (drives "ending soon" reminders).
  - **placeholder** = minimal pledge to access the pledge manager; may never complete; **excluded from
    active pipeline/ETA views** until committed.
- Overall status is **derived** from its waves.

### Wave (a shipment)
- **dates + ETA/delay (derived)**, **address** (moves between waves; history via simple-history),
  **tracking links**, **`delivery_type` ∈ {physical, digital}**, status incl. terminal
  **never-arrived / cancelled** (publisher bankruptcy etc.).
- A Purchase auto-creates "Wave 1"; add/split waves only when a campaign ships in parts.
- **Digital wave**: no shipping/address/tracking; delivers file **links** after fulfillment. A fully-PnP
  campaign is just a purchase whose only wave is digital. Digital-wave game products enter the
  collection with a **PnP-flagged Edition** (`is_pnp` lives on Edition, not Game — you can own the same
  title as a PnP copy and a store copy at once), which **still counts for sleeves** if printed.

### Product (line item)
- Belongs to a wave. Either a **non-game item** or a **game** (optional FK to Game/Edition).
- Game products carry provisional physical/sleeve attributes and, **on arrival, convert into a Copy**.
- No independent status — a product becomes a Copy on arrival or dies with a failed wave.

### Money
- **Deferred.** No money UI/fields in v1. Schema leaves room to add per-**Product** cost later, designed
  so per-item costs can be shown **without ever rendering a grand total**.

---

## 7. Files / documents

- **Document**: `type ∈ {rulebook, PnP file, reference sheet, insert plan, other}`, carrying **an external
  URL and/or an uploaded file** — both can coexist on one record (publisher's official link + the file
  you downloaded).
- Attaches to **Game/Edition** (rulebooks, references) and **Purchase/Wave** (digital deliverables, PnP).
  Generic relation for future attach points.
- Storage: **local filesystem** volume (`/data/media`, docker-mounted), per-file size cap + allowed-type
  list in settings. Optional S3-compatible backend later. External-link-only docs cost no storage.
- Replaces the old Google-Drive-folders workflow; each game page shows its document list.

---

## 8. BGG integration

- **Auth (two phases):** BGG (2025) put the XML API behind auth, but **exempts downloading your own
  collection** from app registration. **Phase 1 (current, shipped):** log in with an admin **BGG
  username/password** (`/login/api/v1` → session cookies) and pull the owner's own collection —
  `sync_bgg` does this today. **Phase 2 (when a token is granted):** swap *only* the login step for a
  registered-app `Authorization: Bearer <token>` — the seam is isolated to `BggClient.login()`. The
  token is **not** about richer *collection* data (those XML fields are identical); it unlocks
  **multi-user fan-out** (pulling *other* users' collections is outside the personal-use exemption), a
  real **rate-limit allowance**, **`/thing`** (confirmed 401 over a session — §15, so **weight** and
  stats for games outside the collection are token-gated), and **private collections/plays**. Model: **one admin-configured instance credential** (username/password now,
  token later) + **per-user BGG username**. **xmlapi2 itself stays read-only forever** (writing
  plays/collection is not a supported XML API2 operation — a "push once §15 write access exists" idea was
  raised and corrected once already; the write path below is a *separate*, unofficial surface).
- **Collection write-back (issue #117, verified live against real BGG — issue #157):** the app can also
  push a status change up to BGG. The originally guessed `geekcollection.php` POST turned out to be dead
  (Cloudflare-blocks scripted access, and its `action` verbs misbehave even from a real browser); the
  confirmed working path is BGG's own current frontend REST API instead, over the same logged-in session
  cookies `login()` already sets up — no app-registration token involved: resolve the session's own numeric
  userid (`GET /api/users/current`), read the game's current collection item (`GET /api/collections`), then
  PUT the **full item back** with its `status` object replaced wholesale (`PUT /api/collectionitem/<collid>`
  — not a minimal diff, verified live, including a real own→prevowned→own round-trip). This is **not** wired
  as a manual status picker; it's **derived from actions the app already has**: adding/converting a Copy
  pushes *own*, archiving a Copy pushes *previously owned* (only when no other active Copy of that game
  remains), and adding/removing a `WishlistEntry` pushes/clears *wishlist*. **Preordered has no
  push yet** — it's a `Purchase → Wave → Product` chain, not a Copy/WishlistEntry toggle, so it's left for
  a follow-up issue tied to that pipeline. **Adding a brand-new collection item (a game BGG has no entry
  for at all) is also still unverified and deliberately refused** rather than guessed — every push target
  above only ever concerns a game already somewhere in the BGG collection. Failures surface as a
  `BggSyncDiff` row instead of failing the triggering action. Scoped to the single synced account, like
  everything else here.
- **BGG = source of truth for collection membership** on read. The app pulls each user's BGG collection via
  the instance credential and reconciles into Copies (own-collection now; multi-user with the phase-2
  token). A just-pushed status is treated as authoritative for a short confirmation window (so the
  read-sync doesn't stale-clear or diff-flag our own write before BGG's export catches up), after which BGG
  resumes as source of truth. **App-only data is always preserved** across syncs (editions, sleeves,
  keep-status, purchase links, notes — BGG can't represent these).
- **Reconciliation: notify on diffs, never auto-remove.** Game on BGG but not in app → suggest adding.
  Game in app but gone from BGG → flag for review (may be an app-only preorder, or culled on BGG) but it
  **stays** until the user decides.
- **Plays:** display-only, shown per game / recent feed. No editing. The always-on cheap signal is
  `bgg_numplays` (a count) already carried by the token-free `/collection`. Full play *history* has two
  possible sources, and we defer the choice: **(a) BGG `/plays` XML API** — automated pull, but only
  the plays posted back to BGG (via BG Stats' auto-post), thin fields, and gated behind the phase-2
  Bearer token (private plays need it). **(b) BG Stats JSON export** — the user's authoritative copy
  (Settings → Export → `.json`: id-linked Games/Players/Locations/Plays arrays, each game carrying
  `bggId` to join onto `Game.bgg_id`), far richer (scores, winners, locations, per-player detail), no
  BGG auth at all — but there is **no BG Stats public API**, so it's a drop-the-file importer modeled on
  the Mastersheet flow, refreshed manually. **Decision: wait for the phase-2 token and try (a) first;**
  (b) is the fallback if `/plays` proves too thin or the token slips — not worth building a second
  importer for the ~week before the token is expected.
- **Wishlist:** read from BGG (collection endpoint returns wishlist items) and, since issue #117, also a
  push target — adding/removing a `WishlistEntry` pushes/clears *wishlist* on BGG (see write-back above).
- **Sync engine:** background Celery beat (scheduled refresh, e.g. weekly) + manual "refresh this game"
  button; respects rate limits and the 202-queued/retry-with-backoff behavior; runs at `concurrency=1`.
- **Multiple BGG ids:** the **primary** id drives synced stats/image; secondary ids are stored links,
  not aggressively synced.

### New-expansion tracking
- During each owned base game's sync, fetch its expansion list (`boardgameexpansion` links) and **record
  a first-seen timestamp** per expansion.
- Expansions appearing **after** initial sync (or with a recent `yearpublished`) are flagged
  **"new expansion available for [game you own]."**
- Surfaced on the **dashboard** + optional **email**, with actions: **dismiss / mark seen** (per-user —
  not interested, never nag again) or **add to wishlist/purchase pipeline**.

---

## 9. Collection organization

- **Location** — a named place with a type (`storage | other`). A Copy has a **current location**.
  simple-history gives the movement log ("where has this been"). Games stored inside other games' boxes
  are captured via a location note.
- **Loan (issue #43, 2026-07)** — lending/borrowing is tracked independently of Location, so it no longer
  needs a dedicated per-borrower Location: a Loan (`lent-out | borrowed-in`) points at a Copy and carries
  a counterparty that is **either** a registered app User **or** a free-text name (the other party may
  not use the app at all), plus since/expected-return dates and a returned-at date (rows are kept, not
  deleted, so a copy's loan history survives across relends). A **borrowed-in** copy is present in the
  collection (playable, shelved) but denormalized as `Copy.is_borrowed_in` so it's excluded from owned
  stats/culling and exempt from the one-copy-per-edition constraint; **returning** it archives the Copy
  (retaining play history, which is keyed off Game, not Copy). A **lent-out** copy still counts as owned
  — only its availability is affected — matching the pre-#43 Location-based behavior.
- **Box dimensions + size category** on Edition.
- **Kallax % shelf-space math: deferred** — computed later from box dims if missed, not tracked.

---

## 10. Taxonomy & filtering

- **Mechanics = BGG-synced tags** (M2M `Tag(kind=mechanic)`); the user stops maintaining them by hand.
- **Themes = the user's own curated vocabulary** (`Tag(kind=theme)`) — BGG doesn't have this set.
  **Adapts-from (book/comic/film/videogame) fold in as themes.**
- **First-class curated fields** (BGG doesn't give these cleanly, and they're filtered on constantly):
  - **game-type** (competitive / coop / solo / semi-coop / team / traitor / 1vsAll) — multi-select.
    *(**solo** stays here even though it's a player-*count* fact, not an interaction mode — issue #124.
    It records what `min_players` can't: a designed solo mode for a 2+ player game, plus the
    native / optional (`opt`) / app-only (`app`) qualifier. Don't derive it from `min_players == 1`.)*
  - **campaign-structure** (campaign / legacy / scenarios / one-off).
  - **language-dependency** — a single 5-level field (no text / trivial / easy / medium / difficult)
    covering both "how language-dependent is the text" and "how hard for a non-English-speaker"
    (issue #2, 2026-07: these were two overlapping fields; collapsed after confirming
    difficulty-for-non-speakers is the richer, more-populated vocabulary — see `Game.LanguageDependency`
    and `import_taxonomy.py`'s merge rule).
  - **app** + **app version**, **soundtrack (ambience)**, **soundtrack (timer)**.
  - **player-conflict** — single value **+ note** (v1). Coop/competitive duality is captured by the
    multi-select game-type; full per-mode variant modeling is deferred.
- **Digital implementations** (Android / Steam / Board Game Arena) — lightweight per-game links (from the
  old APPs sheet).

### GameChooser (the filter screen)
Axes: **players (effective) · playtime (will/could fit) · game-type · mechanic · theme · weight ·
played-status · location · availability.** Live filtering via htmx (sliders/toggles update results without
full reload; `hx-push-url` keeps the filtered view bookmarkable).

---

## 11. Curation, dashboard & notifications

### Curation / culling
Per-Copy personal fields (excitement, immune, 1-in-1-out, why-it-might-leave, play-until-or-it-leaves)
feed a dedicated **"cull candidates" view** (sort by low excitement, not-immune,
"why it might leave" filled in). **Excitement replaces rating** and is the primary cull signal.
**Archive** (§4) is the endpoint of this lifecycle. The table also shows each copy's game's
**last played** (derived from §8 Play history, "never" if none) and its Game/Last
played/Excitement/Keep columns are user-sortable, defaulting back to the cull-priority
order above (issue #40).

### Dashboard — "needs attention"
Widgets: **pledge managers closing soon · campaigns ending soon · incoming waves · sleeve shortfall ·
unreviewed BGG sync diffs · new expansions.**

### Email (v1, Purelymail SMTP)
Reminders for **pledge-manager deadlines** and **campaigns ending soon** (watched-but-unbacked).
**No overdue-wave reminders** in v1. Sent from Celery beat.

### ntfy push (v1, complements email) (issue #162)
Same two reminder kinds as the email digest, pushed to the user's self-hosted **ntfy** server —
**complements** email rather than replacing it (email keeps working even for users who don't set up
ntfy). **Per-user topic**, not a single shared one: each user's `Membership.ntfy_topic` (Settings
page) routes their reminders to their own topic, so a multi-user deployment never leaks one
person's deadlines to another. The server URL (`NTFY_SERVER_URL`) is a single instance-wide,
env-backed setting — blank by default, so the push is fully opt-in and network-silent until
configured. Fails soft: an unreachable ntfy server never blocks the email or the beat task.
New-expansion / BGG-sync-diff notifications stay dashboard-only in v1 — not pushed.

---

## 12. Import from the Mastersheet

- **One-time, re-runnable management command**: `manage.py import_mastersheet <path.xlsx> --user <you>`
  (not a permanent in-app importer). All imported copies are assigned to the specified user + their group.
- **Two-phase enrichment:** parse each Overview row for **name + BGG id + personal fields** → create
  Games/Editions/Copies → **BGG sync backfills** stats/mechanics/images/categories. The importer only
  carries what BGG can't: excitement, keep-status, language fields, themes, game-type, location,
  upgrades, sleeve data.
- **Scope in:** Overview (games/editions/copies + personal taxonomy), Sleeves (card sizes, inventory in
  packs, per-edition requirements), (Pre)orders (→ Purchases/Waves/Products).
- **Scope out:** Minis (deferred), Plays (BGG), Stats/Utils (derived/helper).
- **Honest about messiness:** imports the cleanly-mappable ~90% and **emits a report of skipped/ambiguous
  rows** for manual fixup (patch the importer and re-run, or hand/AI-fix). Idempotent-ish: upsert by BGG
  id so re-runs don't duplicate.

---

## 13. Visual identity

- Must look **distinct from the other work app**. Same component base (Bootstrap 5.3) but its own skin.
- **Dark mode.** When building, shortlist a few Bootstrap dark themes and choose together.
- **Image-forward:** game **cover-art grids** as the primary browse view (not text tables), large hero
  images on detail pages, custom accent color and typography.

---

## 14. Deferred / out of scope (v1)

- Minis & painting tracking (schema seam left on Copy).
- Dice-type inventory ("how many distinct dice do I own").
- Kallax shelf-space math (computed later from box dims).
- Money / spend tracking (schema room left on Product).
- S3 media backend.
- Videogame collection / play tracking (videogame **purchases** are in scope via the generic Purchase).
- In-app play logging (plays come from BGG).
- Multi-group membership per user.
- Per-mode / per-variant attribute modeling (player-conflict is single-value + note).
- Overdue-wave email reminders.

---

## 15. Open implementation-time verifications

- **Does `/thing` accept a plain logged-in session? — ANSWERED (no), 2026-07-03.** Probed live:
  `/collection` for the logged-in user's own account returns 200 with the session, but `/thing`
  returns `401 Unauthorized` *identically* with or without the session cookies — it is gated by app
  registration, not login. So weight (which lives only on `/thing`) and stats for games outside the
  collection are unreachable token-less; the registered-app Bearer token (§8 phase 2) is the only
  unblock. `sync_bgg` still runs the pass (one request confirms the 401) and degrades to
  collection-only data with a note. Confirmed too: the collection payload carries `<numplays>` (play
  counts) and the wishlist/wanttoplay status flags — both now synced token-free — but never
  `averageweight`.
- ~~Confirm whether BGG's XML API2 now returns card/sleeve data on `thing?versions=1`.~~ **Answered
  (issue #131, 2026-07-06):** it does not — only box dimensions ride along, and no scrapeable source
  exists either. See §5.
- Confirm current BGG token application process, rate limits, and the exact private-collection/plays
  access rules under the new auth scheme (§8 phase 2).
- Pick the concrete Bootstrap dark theme (§13).

---

## 16. Accessories

Tracks game accessories (issue #89): playmats, upgraded tokens/coins, 3D inserts, card dividers,
sleeves-as-products, standalone add-ons that aren't games or expansions.

Two models, mirroring the Game/Copy and SleeveProduct/SleeveInventory split:

- **Accessory** — the catalog row, reused across users. `name`/`brand`, optional `game` **or**
  `edition` FK (mutually exclusive — set `game` when the accessory applies to every printing, `edition`
  when it's printing-specific, leave both blank for a standalone/generic accessory), and a light BGG
  identity/display block (`bgg_id`, `bgg_name`, `image_url`, `bgg_rating`, `last_synced_at`) — accessories
  have their own BGG "boardgameaccessory" pages, but **no automatic sync engine exists yet**; these
  fields are populated by hand in admin.
- **AccessoryCopy** — the owned instance, keyed like Copy/SleeveInventory (`owner` + `accessory`).

Deliberately **not** modelled here: `Copy`'s existing `insert_3d`/`card_dividers`/`accessories_3d`/
`other_accessories`/`upgrades_note` fields are untouched — they answer "do I need/have this kind of
upgrade" (a per-copy status flag), which is a different question from "what discrete Accessory product
do I own." `Product.kind = ACCESSORY` already existed (DESIGN §6); this adds `Product.accessory_copy`
(mirrors `Product.copy`) as the conversion seam, but linking a purchased accessory to its AccessoryCopy
is admin-only for now — no UI, no automatic BGG sync. Deferred (§14-style, follow-up issues):

- Automatic BGG sync for the Accessory identity fields.
- A one-click "convert to AccessoryCopy" action mirroring the existing Product→Copy conversion view.
- User-facing browsing/management pages (list/detail/add/edit) for Accessory/AccessoryCopy.
- An optional link from Copy's upgrade-status fields to the Accessory that fulfills them.
