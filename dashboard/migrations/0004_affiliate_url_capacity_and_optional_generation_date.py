from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0003_alter_linkafiliado_options_and_more"),
    ]

    operations = [
        migrations.AlterField(
            model_name="produto",
            name="product_url",
            field=models.URLField(
                max_length=2048,
                unique=True,
                verbose_name="URL original",
            ),
        ),
        migrations.AlterField(
            model_name="linkafiliado",
            name="original_url",
            field=models.URLField(max_length=2048, verbose_name="URL original"),
        ),
        migrations.AlterField(
            model_name="linkafiliado",
            name="affiliate_url",
            field=models.URLField(max_length=2048, verbose_name="URL afiliada"),
        ),
        migrations.AlterField(
            model_name="linkafiliado",
            name="generated_at",
            field=models.DateTimeField(
                blank=True,
                default=None,
                null=True,
                verbose_name="Data de geração",
            ),
        ),
    ]
