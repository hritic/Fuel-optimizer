from django.core.validators import MinValueValidator
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("route_planner", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="fuelstation",
            name="source_key",
            field=models.CharField(blank=True, max_length=64, null=True, unique=True),
        ),
        migrations.AlterField(
            model_name="fuelstation",
            name="latitude",
            field=models.FloatField(blank=True, db_index=True, null=True),
        ),
        migrations.AlterField(
            model_name="fuelstation",
            name="longitude",
            field=models.FloatField(blank=True, db_index=True, null=True),
        ),
        migrations.AlterField(
            model_name="fuelstation",
            name="price_per_gallon",
            field=models.DecimalField(
                db_index=True,
                decimal_places=4,
                max_digits=8,
                validators=[MinValueValidator(0)],
            ),
        ),
        migrations.AddIndex(
            model_name="fuelstation",
            index=models.Index(fields=["latitude", "longitude"], name="fuel_station_lat_lon_idx"),
        ),
    ]