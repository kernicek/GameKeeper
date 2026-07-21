# Re-points CopySleeveStatus at its edition's SleeveRequirement instead of a
# CardSize of its own (issue #3), so the card size lives in exactly one place
# and can't drift out of sync when a requirement's size is edited. Existing
# statuses are mapped to the requirement that already matches their
# (edition, card_size); statuses with no matching requirement are orphans
# under the old schema and are dropped — the same rule the new schema now
# enforces going forward via on_delete=CASCADE.

from django.db import migrations, models
import django.db.models.deletion


def link_requirements(apps, schema_editor):
    CopySleeveStatus = apps.get_model("gamekeeper", "CopySleeveStatus")
    SleeveRequirement = apps.get_model("gamekeeper", "SleeveRequirement")
    for status in CopySleeveStatus.objects.select_related("copy__edition"):
        requirement = SleeveRequirement.objects.filter(
            edition_id=status.copy.edition_id, card_size_id=status.card_size_id,
        ).first()
        if requirement is None:
            status.delete()
        else:
            status.requirement_id = requirement.pk
            status.save(update_fields=["requirement"])


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('gamekeeper', '0004_designer_game_designers'),
    ]

    operations = [
        migrations.AddField(
            model_name='copysleevestatus',
            name='requirement',
            field=models.ForeignKey(
                null=True, on_delete=django.db.models.deletion.CASCADE,
                related_name='copy_statuses', to='gamekeeper.sleeverequirement',
            ),
        ),
        migrations.RunPython(link_requirements, noop),
        migrations.RemoveConstraint(
            model_name='copysleevestatus',
            name='unique_sleeve_status_per_copy_size',
        ),
        migrations.RemoveField(
            model_name='copysleevestatus',
            name='card_size',
        ),
        migrations.AlterField(
            model_name='copysleevestatus',
            name='requirement',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name='copy_statuses', to='gamekeeper.sleeverequirement',
            ),
        ),
        migrations.AddConstraint(
            model_name='copysleevestatus',
            constraint=models.UniqueConstraint(
                fields=['copy', 'requirement'],
                name='unique_sleeve_status_per_copy_requirement',
            ),
        ),
    ]
