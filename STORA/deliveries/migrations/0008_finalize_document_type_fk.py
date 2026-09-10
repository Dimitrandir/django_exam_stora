from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('deliveries', '0007_populate_document_type_fk'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='deliveryattributes',
            name='document_type',
        ),
        migrations.RenameField(
            model_name='deliveryattributes',
            old_name='document_type_fk',
            new_name='document_type',
        ),
        migrations.AlterField(
            model_name='deliveryattributes',
            name='document_type',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name='deliveries',
                to='deliveries.documenttype',
                verbose_name='Document Type',
            ),
        ),
    ]
