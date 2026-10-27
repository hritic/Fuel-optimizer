import csv
import hashlib
import time
from decimal import Decimal, InvalidOperation

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from route_planner.models import FuelStation
from route_planner.services import RouteOptimizerService, UpstreamServiceError

class Command(BaseCommand):
    help = "Imports fuel prices and optionally enriches missing station coordinates."

    def add_arguments(self, parser):
        parser.add_argument("csv_file", type=str, help="Path to the fuel prices CSV file")
        parser.add_argument(
            "--limit",
            type=int,
            help="Maximum number of valid, unique fuel stations to import and geocode.",
        )
        parser.add_argument(
            "--geocode-missing",
            action="store_true",
            help="Geocode records without coordinates at no more than one request per second.",
        )
        parser.add_argument(
            "--geocode-delay-seconds",
            type=float,
            default=1.1,
            help="Minimum delay between optional geocoding requests.",
        )

    @staticmethod
    def _value(row, *field_names):
        for field_name in field_names:
            value = row.get(field_name)
            if value and value.strip():
                return value.strip()
        return ""

    @classmethod
    def _coordinate(cls, row, *field_names):
        raw_value = cls._value(row, *field_names)
        if not raw_value:
            return None
        try:
            return float(raw_value)
        except ValueError:
            return None

    @classmethod
    def _source_key(cls, row, name, address, city, state):
        source_id = cls._value(row, "OPIS Truckstop ID", "Truckstop ID", "Store ID", "id")
        fingerprint = "|".join(
            value.casefold().strip() for value in (source_id, name, address, city, state)
        )
        return hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()

    def _parse_records(self, file_path, limit=None):
        records = {}
        skipped_rows = 0
        try:
            with open(file_path, mode="r", encoding="utf-8-sig", newline="") as csv_file:
                reader = csv.DictReader(csv_file)
                for row in reader:
                    name = self._value(row, "Truckstop Name", "Store Name", "Name")
                    address = self._value(row, "Address")
                    city = self._value(row, "City")
                    state = self._value(row, "State")
                    price_raw = self._value(row, "Retail Price", "Price", "price_per_gallon")
                    try:
                        price = Decimal(price_raw)
                    except (InvalidOperation, ValueError):
                        skipped_rows += 1
                        continue

                    if (
                        not name
                        or not city
                        or not state
                        or not price.is_finite()
                        or price < 0
                        or price > Decimal("9999.9999")
                    ):
                        skipped_rows += 1
                        continue

                    latitude = self._coordinate(row, "Latitude", "latitude", "Lat")
                    longitude = self._coordinate(row, "Longitude", "longitude", "Lon", "Lng")
                    if (
                        latitude is None
                        or longitude is None
                        or not -90 <= latitude <= 90
                        or not -180 <= longitude <= 180
                    ):
                        latitude = None
                        longitude = None

                    source_key = self._source_key(row, name, address, city, state)
                    records[source_key] = {
                        "source_key": source_key,
                        "name": name,
                        "address": address,
                        "city": city,
                        "state": state,
                        "latitude": latitude,
                        "longitude": longitude,
                        "price_per_gallon": price,
                    }
                    if limit is not None and len(records) >= limit:
                        break
        except OSError as exc:
            raise CommandError(f"Could not read {file_path}: {exc}") from exc

        return records, skipped_rows

    def _geocode_missing_records(self, records, delay_seconds):
        unresolved_records = [
            record
            for record in records.values()
            if record["latitude"] is None or record["longitude"] is None
        ]
        if not unresolved_records:
            return

        self.stdout.write(
            self.style.WARNING(
                "Geocoding missing stations one at a time. Use a self-hosted or licensed geocoder for large datasets."
            )
        )
        for index, record in enumerate(unresolved_records):
            query = ", ".join(
                value for value in (record["city"], record["state"]) if value
            )
            try:
                latitude, longitude = RouteOptimizerService.geocode_location(query)
            except UpstreamServiceError:
                latitude, longitude = None, None

            if latitude is not None and longitude is not None:
                record["latitude"] = latitude
                record["longitude"] = longitude

            if index < len(unresolved_records) - 1:
                time.sleep(max(delay_seconds, 1.0))

    def handle(self, *args, **options):
        limit = options["limit"]
        if limit is not None and limit <= 0:
            raise CommandError("--limit must be greater than zero.")

        records, skipped_rows = self._parse_records(options["csv_file"], limit)
        if not records:
            raise CommandError("No valid fuel-price records were found in the CSV file.")

        if limit is not None or options["geocode_missing"]:
            self._geocode_missing_records(records, options["geocode_delay_seconds"])

        existing_stations = FuelStation.objects.in_bulk(records.keys(), field_name="source_key")
        stations_to_create = []
        stations_to_update = []
        for source_key, record in records.items():
            station = existing_stations.get(source_key)
            if station is None:
                stations_to_create.append(FuelStation(**record))
                continue

            for field_name, value in record.items():
                if field_name in {"latitude", "longitude"} and value is None:
                    continue
                setattr(station, field_name, value)
            stations_to_update.append(station)

        with transaction.atomic():
            if stations_to_create:
                FuelStation.objects.bulk_create(stations_to_create, batch_size=500)
            if stations_to_update:
                FuelStation.objects.bulk_update(
                    stations_to_update,
                    [
                        "name",
                        "address",
                        "city",
                        "state",
                        "latitude",
                        "longitude",
                        "price_per_gallon",
                    ],
                    batch_size=500,
                )

        unresolved_count = sum(
            1
            for record in records.values()
            if record["latitude"] is None or record["longitude"] is None
        )
        self.stdout.write(
            self.style.SUCCESS(
                "Imported "
                f"{len(stations_to_create)} new and {len(stations_to_update)} updated fuel stations; "
                f"{unresolved_count} remain without coordinates; {skipped_rows} rows were skipped."
            )
        )
