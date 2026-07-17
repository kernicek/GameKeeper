"""One-off live diagnostic for the BGG collection write-back API (issue
#157, continuing #117): confirms BggClient.get_user_id / get_collection_item
/ put_collection_item work end-to-end against a real, logged-in BGG session.
This command is NOT part of the app or its test suite — it makes real
network calls against the operator's own BGG account and is meant to be run
by hand. Nothing here is imported by any other module.

Safety model: the write step never invents a status change. It PUTs the
target item back byte-for-byte as read (an idempotent round-trip), so a
successful test provably restores the exact pre-test state — there is
nothing to revert. If the item isn't in the collection at all yet, there is
nothing to replay, so the command stops after the read: pushing a brand-new
collection item is a still-open question (see push_bgg_status's docstring)
and deliberately untested here too.

The account password is only ever handed to BggClient (via make_bgg_client,
bgg_sync.py:125-131) — never printed, logged, or written to any file here.
"""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from gamekeeper.bgg import BggAuthError, BggError
from gamekeeper.bgg_sync import bgg_credentials_error, make_bgg_client
from gamekeeper.models import Game


class Command(BaseCommand):
    help = (
        "Live-verify the BGG collection write-back REST API (issue #157) "
        "against a real BGG session. Manual diagnostic only — never run by "
        "CI or the scheduled sync."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--user", required=True,
            help="App username whose BGG credentials to use (Membership or env fallback).",
        )
        parser.add_argument(
            "--game-name", default="Above and Below",
            help="Game (matched by name) whose BGG item is the test subject.",
        )
        parser.add_argument(
            "--confirm-live-write", action="store_true",
            help="Actually PUT the item back to BGG. Without this, only reads happen.",
        )

    def handle(self, *args, **options):
        User = get_user_model()
        try:
            user = User.objects.get(username=options["user"])
        except User.DoesNotExist:
            raise CommandError(f"User {options['user']!r} does not exist.")

        if error := bgg_credentials_error(user):
            raise CommandError(error)

        try:
            game = Game.objects.get(name__iexact=options["game_name"])
        except Game.DoesNotExist:
            raise CommandError(f"No game named {options['game_name']!r}.")
        except Game.MultipleObjectsReturned:
            raise CommandError(f"Multiple games named {options['game_name']!r} — narrow it down.")
        link = game.primary_bgg_link
        if link is None:
            raise CommandError(f"{game.name!r} has no primary BGG link.")
        bgg_id = link.bgg_id
        self.stdout.write(f"Test subject: {game.name!r} -> BGG id {bgg_id}.")

        client = make_bgg_client(user)
        try:
            client.login()
            userid = client.get_user_id()
        except (BggAuthError, BggError) as error:
            raise CommandError(str(error))
        self.stdout.write(f"Logged into BGG. Resolved userid={userid}.")

        item = self._read_item(client, bgg_id, userid, label="Baseline")
        if item is None:
            self.stdout.write(self.style.WARNING(
                f"BGG id {bgg_id} is NOT currently in userid {userid}'s "
                "collection — nothing to replay. Adding a brand-new "
                "collection item is untested (issue #157) and out of scope "
                "for this command."
            ))
            return

        if not options["confirm_live_write"]:
            self.stdout.write(self.style.WARNING(
                "Dry run — no write attempted. Re-run with --confirm-live-write "
                "to PUT the item back to BGG (an idempotent no-op replay)."
            ))
            return

        self._live_write(client, bgg_id, userid, item)

    def _read_item(self, client, bgg_id, userid, *, label):
        try:
            item = client.get_collection_item(bgg_id, userid)
        except BggError as error:
            raise CommandError(f"{label} read failed: {error}")
        if item is None:
            return None
        self.stdout.write(
            f"{label}: collid={item['collid']}, status={item['status']}, "
            f"wishlistpriority={item.get('wishlistpriority')}"
        )
        return item

    def _live_write(self, client, bgg_id, userid, baseline):
        """The one real PUT: replays `baseline` byte-for-byte (a provably
        reversible no-op), then re-reads to confirm the round-trip."""
        self.stdout.write(f"PUTting collectionitem/{baseline['collid']} back unchanged ...")
        try:
            body = client.put_collection_item(baseline)
        except BggError as error:
            raise CommandError(f"Live write failed: {error}")
        self.stdout.write(f"Response: {body}")

        confirm = self._read_item(client, bgg_id, userid, label="Post-write")
        if confirm is not None and confirm["status"] == baseline["status"]:
            self.stdout.write(self.style.SUCCESS(
                "Post-write read matches baseline — round-trip confirmed."
            ))
        else:
            self.stdout.write(self.style.WARNING(
                f"Post-write read DIFFERS from baseline.\n"
                f"  baseline={baseline['status']}\n"
                f"  confirm={confirm['status'] if confirm else None}"
            ))