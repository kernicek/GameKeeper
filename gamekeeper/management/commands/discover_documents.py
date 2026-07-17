"""Adopt document files already sitting on disk (DESIGN §7, issue #60).

The §7 Document model normally gets its files through the web upload, which
writes them into media/documents/<host tree>/ under a human-readable,
id-suffixed layout ('games/Wingspan [42]/Rulebook.pdf'). This command walks
that same tree and creates a Document row for every file that isn't already
tracked — so the old Google-Drive folders can just be copied into the volume
and adopted in bulk, and re-run safely as more files land.

The naming contract is exactly what models.document_upload_path writes:

    games/<base> [<pk>]/<file>                       -> Game
    games/<base> [<pk>]/<edition> [<pk>]/<file>      -> Edition (of the base)
    games/<base> [<pk>]/<expansion> [<pk>]/<file>    -> Game (expansion, #99)
    games/<base> [<pk>]/<expansion> [<pk>]/<edition> [<pk>]/<file>
                                                     -> Edition (of the expansion)
    series/<name> [<pk>]/<file>                      -> Series
    purchases/<name> [<pk>]/<file>                   -> Purchase
    purchases/<name> [<pk>]/wave-<n>/<file>          -> Wave

Files don't get copied: the row's FileField just points at the file where it
already lives. Anything whose path doesn't parse (unknown top folder, no
trailing [id], a pk that resolves to no row) is reported and skipped, never
guessed.
"""

import os
import re
from pathlib import Path, PurePosixPath

from django.conf import settings
from django.core.management.base import BaseCommand

from gamekeeper.models import (
    Document, Edition, Game, Purchase, Series, Wave,
)

# The trailing ' [<pk>]' on an id-bearing folder segment.
_PK_SUFFIX = re.compile(r"\[(\d+)\]\s*$")

DECISION_NOTES = [
    "Files are matched by the '<name> [<pk>]' folder layout that uploads "
    "write, so the pk (not the name) identifies the host — a renamed game "
    "still adopts correctly.",
    "Adoption is in-place: the Document's file points at the existing file, "
    "nothing is copied or moved.",
    "Already-tracked files are skipped, so the command is idempotent — copy "
    "more folders in and re-run.",
    "Unparseable paths are reported and skipped, never guessed at.",
]


def _pk_from_segment(segment):
    """The pk in a '<name> [<pk>]' folder segment, or None."""
    match = _PK_SUFFIX.search(segment)
    return int(match.group(1)) if match else None


def _resolve_host(segments):
    """Map the folder segments under documents/ (filename excluded) to a host
    object, or None when the path doesn't fit the naming contract."""
    if not segments:
        return None
    top = segments[0]

    if top == "games":
        if len(segments) < 2:
            return None
        base = _lookup(Game, segments[1])  # base (or standalone) game
        if base is None:
            return None
        if len(segments) == 2:  # file directly under the game folder
            return base
        if len(segments) == 3:
            # Either an edition of the base, or an expansion nested under its
            # base (#99). Edition wins first: it's the pre-#99 layout, and an
            # expansion is only accepted when it's actually linked to the base.
            edition = _lookup(Edition, segments[2])
            if edition is not None and edition.game_id == base.pk:
                return edition
            expansion = _lookup(Game, segments[2])
            if expansion is not None and base in expansion.expands.all():
                return expansion
            return None
        if len(segments) == 4:
            # An expansion's own edition: games/<base>/<expansion>/<edition>.
            expansion = _lookup(Game, segments[2])
            if expansion is None or base not in expansion.expands.all():
                return None
            edition = _lookup(Edition, segments[3])
            if edition is None or edition.game_id != expansion.pk:
                return None
            return edition
        return None  # deeper than the contract describes

    if top == "series":
        if len(segments) != 2:
            return None
        return _lookup(Series, segments[1])

    if top == "purchases":
        if len(segments) < 2:
            return None
        purchase = _lookup(Purchase, segments[1])
        if purchase is None:
            return None
        if len(segments) == 2:
            return purchase
        if len(segments) == 3 and segments[2].startswith("wave-"):
            try:
                number = int(segments[2][len("wave-"):])
            except ValueError:
                return None
            return purchase.waves.filter(number=number).first()
        return None

    return None


def _lookup(model, segment):
    pk = _pk_from_segment(segment)
    if pk is None:
        return None
    return model.objects.filter(pk=pk).first()


class Command(BaseCommand):
    help = "Adopt document files already on disk under media/documents/ (§7)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report what would be adopted without writing any rows.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        media_root = Path(settings.MEDIA_ROOT)
        documents_root = media_root / "documents"
        if not documents_root.is_dir():
            self.stdout.write(
                f"No documents directory at {documents_root} — nothing to do.")
            return

        # Idempotency: never adopt a file some Document already points at.
        tracked = set(
            Document.objects.exclude(file="").values_list("file", flat=True))

        adopted = 0
        skipped_tracked = 0
        unparseable = []

        for dirpath, _dirnames, filenames in os.walk(documents_root):
            for filename in filenames:
                full = Path(dirpath) / filename
                # Storage-relative name (what FileField stores), forward slashes.
                rel_to_media = PurePosixPath(
                    full.relative_to(media_root).as_posix())
                storage_name = str(rel_to_media)
                if storage_name in tracked:
                    skipped_tracked += 1
                    continue

                # Segments under documents/, filename dropped.
                segments = list(rel_to_media.parts)[1:-1]
                host = _resolve_host(segments)
                if host is None:
                    unparseable.append(storage_name)
                    continue

                if dry_run:
                    self.stdout.write(
                        f"would adopt: {storage_name} -> "
                        f"{host._meta.model_name} #{host.pk}")
                    adopted += 1
                    continue

                document = Document(
                    content_object=host,
                    doc_type=Document.Type.OTHER,
                    label=full.stem,
                )
                document.file.name = storage_name
                document.save()
                tracked.add(storage_name)
                adopted += 1
                self.stdout.write(
                    f"adopted: {storage_name} -> "
                    f"{host._meta.model_name} #{host.pk}")

        verb = "would adopt" if dry_run else "adopted"
        self.stdout.write(self.style.SUCCESS(
            f"{verb} {adopted}, skipped {skipped_tracked} already-tracked, "
            f"{len(unparseable)} unparseable."))
        for path in unparseable:
            self.stdout.write(self.style.WARNING(f"  unparseable: {path}"))
