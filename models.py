from django.db import models
from django.core.validators import MinValueValidator


class FuelStation(models.Model):
    source_key = models.CharField(max_length=64, unique=True, null=True, blank=True)
    name = models.CharField(max_length=255)
    address = models.CharField(max_length=255)
    city = models.CharField(max_length=100)
    state = models.CharField(max_length=10)
    latitude = models.FloatField(db_index=True, null=True, blank=True)
    longitude = models.FloatField(db_index=True, null=True, blank=True)
    price_per_gallon = models.DecimalField(
        max_digits=8,
        decimal_places=4,
        db_index=True,
        validators=[MinValueValidator(0)],
    )

    class Meta:
        indexes = [
            models.Index(fields=["latitude", "longitude"], name="fuel_station_lat_lon_idx"),
        ]

    def __str__(self):
        return f"{self.name} - {self.city}, {self.state} (${self.price_per_gallon}/gal)"
