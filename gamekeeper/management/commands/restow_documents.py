"""Relocate document files to their canonical §7 folder (issue #99).

When #99 re-rooted an expansion's documents under its base game's folder, the
naming contract that `models.document_upload_path` writes changed, but files
already on disk (and the Document rows pointing at them) kept their old paths.
This command reconciles the two: for every Document with a file, it recomputes
the canonical path from the live host and, when it differs from where the file
sits today, moves the file on disk and updates the row's `file.name`.

It's the sibling of `discover_documents` — a one-shot admin command, matching §7's
manual operational model — and safe to re-run: a file already at its canonical
path is left untouched, so re-runs are idempotent. Beyond the #99 migration it
also heals any host whose canonical folder later changes (e.g. an expansion newly
linked to a base).
"""

import os
import shutil
from pathlib import Path, PurePosixPath

from django.conf import settings
from django.core.management.base import BaseCommand

from gamekeeper.models import Document, _document_host_folder


def _canonical_name(document):
    """The storage-relative path this document's file *should* have, keeping its
    current basename (the folder is what #99 moved, not the filename)."""
    basename = PurePosixPath(document.file.name).name
    folder = _document_host_folder(document.content_object)
    return f"documents/{folder}/{basename}"


class Command(BaseCommand):
    help = "Move document files to their canonical §7 folder layout (#99)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report what would move without touching disk or rows.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        media_root = Path(settings.MEDIA_ROOT)

        moved = 0
        already = 0
        missing = []

        for document in Document.objects.exclude(file="").select_related(
                "content_type"):
            host = document.content_object
            if host is None:  # dangling generic relation — leave it be
                continue
            current = document.file.name
            target = _canonical_name(document)
            if current == target:
                already += 1
                continue

            source_path = media_root / current
            if not source_path.exists():
                # Row points at a file that isn't there — don't invent a move.
                missing.append(current)
                continue

            if dry_run:
                self.stdout.write(f"would move: {current} -> {target}")
                moved += 1
                continue

            dest_path = media_root / target
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            # #113: keep every created media directory world-traversable so a
            # container running as non-root can still serve the file.
            self._chmod_up(dest_path.parent, media_root)
            shutil.move(os.fspath(source_path), os.fspath(dest_path))
            document.file.name = target
            document.save(update_fields=["file"])
            moved += 1
            self.stdout.write(f"moved: {current} -> {target}")

        verb = "would move" if dry_run else "moved"
        self.stdout.write(self.style.SUCCESS(
            f"{verb} {moved}, {already} already canonical, "
            f"{len(missing)} missing on disk."))
        for path in missing:
            self.stdout.write(self.style.WARNING(f"  missing: {path}"))

    def _chmod_up(self, directory, stop_at):
        """chmod 0o755 from `directory` up to (not past) `stop_at`, ignoring
        failures — ZFS aclmode=restricted vetoes chmod (README), and that's the
        entrypoint's job to heal, not ours to hard-fail on."""
        directory = directory.resolve()
        stop_at = stop_at.resolve()
        while directory != stop_at and stop_at in directory.parents:
            try:
                os.chmod(directory, 0o755)
            except OSError:
                pass
            directory = directory.parent
