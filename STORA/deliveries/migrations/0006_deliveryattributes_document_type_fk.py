from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('deliveries', '0005_seed_document_types'),
    ]

    operations = [
        migrations.AddField(
            model_name='deliveryattributes',
            name='document_type_fk',
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='deliveries',
                to='deliveries.documenttype',
            ),
        ),
    ]
