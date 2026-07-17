# Seeds the built-in pledge managers (issue #181). Pulled out of the old
# per-history 0049 migration when the migration history was squashed to
# 0001_initial for the public release — the schema is now created fresh, but
# this reference data still needs seeding on every install.

from django.db import migrations

# (slug, display name, default url) — mirrors Purchase's old free-form
# pledge_manager choices and the PLEDGE_MANAGER_DEFAULT_URLS dict.
_SEED = [
    ("crowdox", "CrowdOx", "http://portal.crowdox.com/"),
    ("backerkit", "BackerKit", "https://www.backerkit.com/backer_accounts"),
    ("pledgemanager", "PledgeManager", "https://my.pledgemanager.com/"),
    ("pledgit", "Pledg.it", "https://pledg.it/account/pledges"),
    ("gamefound", "Gamefound", "https://gamefound.com/en/users/dashboard"),
    ("pledgebox", "PledgeBox", "https://backer.pledgebox.com/portal/projects"),
    ("kickstarter", "Kickstarter", ""),
    ("other", "Other", ""),
]


def seed_pledge_managers(apps, schema_editor):
    PledgeManager = apps.get_model("gamekeeper", "PledgeManager")
    for _slug, name, url in _SEED:
        PledgeManager.objects.get_or_create(name=name, defaults={"default_url": url})


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('gamekeeper', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(seed_pledge_managers, noop),
    ]
