from rest_framework import serializers


class LocationField(serializers.Field):
    """Accepts "City, ST", a street address, "lat,lng", or {"lat": .., "lng": ..}."""

    invalid_message = 'Expected a string ("Chicago, IL" or "41.88,-87.63") or an object {"lat": .., "lng": ..}.'

    def to_internal_value(self, data):
        if isinstance(data, str):
            if not data.strip():
                raise serializers.ValidationError("This field may not be blank.")
            if len(data) > 255:
                raise serializers.ValidationError("Ensure this field has no more than 255 characters.")
            return data.strip()
        if isinstance(data, dict) and {"lat", "lng"} <= data.keys():
            return {"lat": data["lat"], "lng": data["lng"]}
        # Raised directly: DRF's self.fail() runs messages through str.format, which breaks on braces.
        raise serializers.ValidationError(self.invalid_message)

    def to_representation(self, value):
        return value


class RoutePlanRequestSerializer(serializers.Serializer):
    start = LocationField()
    finish = LocationField()
    stop_penalty = serializers.FloatField(
        required=False,
        min_value=0,
        max_value=1000,
        help_text="USD a stop must save to be worth making (default from settings; 0 = cheapest possible).",
    )
