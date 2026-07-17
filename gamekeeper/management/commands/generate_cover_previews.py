"""Backfill the baked square grid-tile previews for existing covers (#104).

The collection grid serves a pre-rendered square thumbnail (cover_preview)
instead of the full cover_image cropped in CSS. New/edited covers bake it on
the fly (cover replace, focus/zoom/fit edit, download_covers); this command
backfills every CoverArtModel that already has a local cover but no preview.

Idempotent: covers that already have a preview are skipped unless --force.
Runs over Game, Series and Family (all inherit the cover machinery).
"""

from django.core.management.base import BaseCommand

from gamekeeper.models import Family, Game, Series

# Every cover-bearing model (CoverArtModel subclasses) — one preview each.
COVER_MODELS = [Game, Series, Family]


class Command(BaseCommand):
    help = "Bake the square grid-tile cover previews for existing covers (#104)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--force", action="store_true",
            help="Re-bake previews that already exist.",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report what would be baked without writing anything.",
        )

    def handle(self, *args, **options):
        self.counts = {}
        self.skipped = []  # (obj, reason)
        force = options["force"]
        dry_run = options["dry_run"]

        for model in COVER_MODELS:
            for obj in model.objects.order_by("pk"):
                self._generate(obj, force=force, dry_run=dry_run)

        self._print_report(dry_run=dry_run)

    def _generate(self, obj, force, dry_run):
        label = obj._meta.model_name
        if not obj.cover_image:
            return  # no local art — the tile falls back to the full cover
        if obj.cover_preview and not force:
            self._bump(f"{label}: already baked — skipped")
            return
        if dry_run:
            self._bump(f"{label}: would bake preview")
            return
        try:
            obj.regenerate_cover_preview()
        except Exception as error:  # unreadable/missing file — report, don't die
            self.skipped.append((obj, f"could not bake preview: {error}"))
            return
        self._bump(f"{label}: preview baked")

    def _bump(self, key):
        self.counts[key] = self.counts.get(key, 0) + 1

    def _print_report(self, dry_run):
        write = self.stdout.write
        if dry_run:
            write(self.style.WARNING("DRY RUN — nothing was baked.\n"))

        write(self.style.MIGRATE_HEADING("Summary"))
        for key in sorted(self.counts):
            write(f"  {key}: {self.counts[key]}")

        write(self.style.MIGRATE_HEADING("Skipped (failures)"))
        if self.skipped:
            for obj, reason in self.skipped:
                write(f"  {obj!r}: {reason}")
        else:
            write("  none")

        write(self.style.SUCCESS("Done." if not dry_run else "Dry run complete."))
