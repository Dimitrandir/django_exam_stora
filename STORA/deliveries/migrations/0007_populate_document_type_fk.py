from django.db import migrations

OLD_VALUE_TO_NAME = {
    'INVOICE': 'Invoice',
    'DELIVERY_NOTE': 'Delivery Note',
}


def populate(apps, schema_editor):
    DeliveryAttributes = apps.get_model('deliveries', 'DeliveryAttributes')
    DocumentType = apps.get_model('deliveries', 'DocumentType')
    by_name = {dt.name: dt for dt in DocumentType.objects.all()}

    for delivery in DeliveryAttributes.objects.all():
        name = OLD_VALUE_TO_NAME.get(delivery.document_type, delivery.document_type)
        document_type = by_name.get(name)
        if document_type is None:
            # Unexpected/legacy value -- create a matching type rather than
            # silently dropping which document this delivery arrived with.
            document_type, _ = DocumentType.objects.get_or_create(name=name or 'Unknown')
            by_name[document_type.name] = document_type
        delivery.document_type_fk_id = document_type.id
        delivery.save(update_fields=['document_type_fk'])


def unpopulate(apps, schema_editor):
    DeliveryAttributes = apps.get_model('deliveries', 'DeliveryAttributes')
    DeliveryAttributes.objects.update(document_type_fk=None)


class Migration(migrations.Migration):

    dependencies = [
        ('deliveries', '0006_deliveryattributes_document_type_fk'),
    ]

    operations = [
        migrations.RunPython(populate, unpopulate),
    ]
