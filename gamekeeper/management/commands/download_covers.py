"""Download every Game's full-size BGG cover into local media (DESIGN §13).

The §8 sync stores only geekdo-images URLs; the grid used to hotlink the
~200px thumbnails, which upscale blurry and die with BGG outages. This
fetches each Game's full image_url once into media/covers/<bgg id>.<ext>
and points Game.cover_image at it; templates prefer the local file.

Files are never overwritten unless --force: a hand-replaced cover in
media/covers/ survives re-runs (drop in a new file under the same name, or
clear the field and re-run to re-fetch from BGG). Games the sync hasn't
reached (no image_url — mostly preorders) are reported, not failed.
"""

import io
import time
from pathlib import PurePosixPath
from urllib.parse import urlparse

from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand

import requests
from PIL import Image

from gamekeeper.models import Game

REQUEST_TIMEOUT = 30
# geekdo-images is a CDN, but stay polite on ~300 sequential fetches.
DELAY_SECONDS = 0.3

DECISION_NOTES = [
    "The full image_url is fetched, not the ~200px thumbnail — the grid "
    "downscales sharply instead of upscaling blurrily.",
    "Existing files/fields are skipped without --force, so hand-replaced "
    "covers in media/covers/ survive re-runs.",
    "Files are named covers/<primary BGG id>.<ext> (game-<pk> when a game "
    "has no BGG link).",
    "Art dimensions (cover_width/height, issue #1 fit-mode zoom) are "
    "recorded on download and backfilled for skipped existing files.",
]


class Command(BaseCommand):
    help = "Download full-size BGG cover images into local media (DESIGN §13)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--force", action="store_true",
            help="Re-download covers that already have a local file.",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report what would be downloaded without fetching anything.",
        )

    def handle(self, *args, **options):
        self.counts = {}
        self.skipped = []  # (game, reason)

        games = Game.objects.prefetch_related("bgg_links").order_by("name")
        for game in games:
            self._download(game, force=options["force"], dry_run=options["dry_run"])

        self._print_report(dry_run=options["dry_run"])

    def _bump(self, key):
        self.counts[key] = self.counts.get(key, 0) + 1

    def _download(self, game, force, dry_run):
        # Local file first: hand-replaced covers exist on games the sync
        # never reached (no image_url) — they still deserve the dimensions
        # backfill below.
        if game.cover_image and not force:
            if game.cover_image.storage.exists(game.cover_image.name):
                self._backfill_dimensions(game, dry_run)
                return
            # Field set but file gone (wiped media dir): re-fetch.
        if not game.image_url:
            self._bump("no image URL (not synced yet)")
            return
        if dry_run:
            self._bump("would download")
            return

        try:
            response = requests.get(game.image_url, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
        except requests.RequestException as error:
            self.skipped.append((game, f"download failed: {error}"))
            return

        if game.cover_image:
            game.cover_image.delete(save=False)  # --force / dead file: replace
        link = game.primary_bgg_link
        stem = str(link.bgg_id) if link else f"game-{game.pk}"
        extension = PurePosixPath(urlparse(game.image_url).path).suffix or ".jpg"
        game.cover_width, game.cover_height = self._dimensions(response.content)
        game.cover_image.save(f"{stem}{extension}", ContentFile(response.content))
        # Bake the grid thumbnail (issue #104) — but only from art Pillow can
        # actually read (unreadable bytes leave the dims None and no preview).
        if game.cover_width:
            game.regenerate_cover_preview()
        self._bump("downloaded")
        time.sleep(DELAY_SECONDS)

    @staticmethod
    def _dimensions(data):
        """(width, height) of the image bytes, (None, None) when unreadable.
        They feed the aspect-aware fit-mode zoom scale (issue #1)."""
        try:
            with Image.open(io.BytesIO(data)) as image:
                return image.size
        except Exception:
            return None, None

    def _backfill_dimensions(self, game, dry_run):
        """Skipped-existing files still get their dimensions recorded once —
        this is the issue #1 backfill for covers downloaded before the
        cover_width/height fields existed."""
        if game.cover_width and game.cover_height:
            self._bump("already downloaded — skipped")
            return
        if dry_run:
            self._bump("would record dimensions for existing file")
            return
        with game.cover_image.open("rb") as handle:
            width, height = self._dimensions(handle.read())
        if width is None:
            self.skipped.append((game, "existing cover file is unreadable"))
            return
        game.cover_width = width
        game.cover_height = height
        game.save(update_fields=["cover_width", "cover_height", "updated_at"])
        self._bump("dimensions recorded for existing file")

    def _print_report(self, dry_run):
        write = self.stdout.write
        if dry_run:
            write(self.style.WARNING("DRY RUN — nothing was downloaded.\n"))

        write(self.style.MIGRATE_HEADING("Summary"))
        for key in sorted(self.counts):
            write(f"  {key}: {self.counts[key]}")

        write(self.style.MIGRATE_HEADING("Skipped (download failures)"))
        if self.skipped:
            for game, reason in self.skipped:
                write(f"  {game.name!r}: {reason}")
        else:
            write("  none")

        write(self.style.MIGRATE_HEADING("Interpretation decisions"))
        for note in DECISION_NOTES:
            write(f"  - {note}")

        write(self.style.SUCCESS("Done." if not dry_run else "Dry run complete."))
