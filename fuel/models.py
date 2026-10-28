import uuid

from django.db import models


class FuelStation(models.Model):
    """A truck stop from the OPIS price file, deduplicated by OPIS ID.

    The source file lists some stations several times with different prices;
    we keep the cheapest observed price (``price``) and record how many price
    rows were seen (``price_observations``).
    """

    opis_id = models.PositiveIntegerField(unique=True)
    name = models.CharField(max_length=255)
    address = models.CharField(max_length=255, blank=True)
    city = models.CharField(max_length=120)
    state = models.CharField(max_length=2, db_index=True)
    rack_id = models.PositiveIntegerField(null=True, blank=True)
    price = models.DecimalField(max_digits=12, decimal_places=8, help_text="USD per gallon")
    price_observations = models.PositiveSmallIntegerField(default=1)
    latitude = models.FloatField()
    longitude = models.FloatField()
    geocode_source = models.CharField(max_length=32, blank=True)

    class Meta:
        ordering = ["opis_id"]

    def __str__(self) -> str:
        return f"{self.name} ({self.city}, {self.state}) ${self.price}"


class RoutePlan(models.Model):
    """A computed fuel plan, persisted so its map page can be served later."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    start_query = models.CharField(max_length=255)
    finish_query = models.CharField(max_length=255)
    result = models.JSONField()
    route_coordinates = models.JSONField(help_text="Full route geometry as [[lng, lat], ...]")

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.start_query} -> {self.finish_query}"
