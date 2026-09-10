from django.db import migrations

# The old document_type CharField used these two choice keys/labels --
# seeding matching DocumentType rows before the FK swap (0006-0008) so the
# data migration there has something to point existing rows at.
SEED_NAMES = ['Invoice', 'Delivery Note']


def seed(apps, schema_editor):
    DocumentType = apps.get_model('deliveries', 'DocumentType')
    for name in SEED_NAMES:
        DocumentType.objects.get_or_create(name=name)


def unseed(apps, schema_editor):
    DocumentType = apps.get_model('deliveries', 'DocumentType')
    DocumentType.objects.filter(name__in=SEED_NAMES).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('deliveries', '0004_documenttype'),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
