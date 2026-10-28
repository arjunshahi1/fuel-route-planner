from django.shortcuts import get_object_or_404, render
from rest_framework import status
from rest_framework.response import Response
from rest_framework.reverse import reverse
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView

from .models import RoutePlan
from .serializers import RoutePlanRequestSerializer
from .services.planner import PlanningError, build_plan, with_map


def _error(message: str, http_status: int, field: str | None = None) -> Response:
    body = {"error": {"message": message}}
    if field:
        body["error"]["field"] = field
    return Response(body, status=http_status)


def _with_absolute_map_url(request, payload: dict) -> dict:
    return {**payload, "map_url": request.build_absolute_uri(payload["map_url"])}


class ApiRootView(APIView):
    def get(self, request):
        return Response(
            {
                "name": "Fuel Route Planner API",
                "endpoints": {
                    "plan_route": {
                        "method": "POST",
                        "url": request.build_absolute_uri("/api/v1/route-plans/"),
                        "body": {"start": "Chicago, IL", "finish": "Denver, CO"},
                    },
                    "get_plan": "/api/v1/route-plans/<id>/",
                    "map": "/api/v1/route-plans/<id>/map/",
                },
            }
        )


class RoutePlanView(APIView):
    """Plan the cheapest fuel stops for a trip between two US locations."""

    throttle_classes = [AnonRateThrottle]

    def post(self, request):
        serializer = RoutePlanRequestSerializer(data=request.data)
        if not serializer.is_valid():
            field, messages = next(iter(serializer.errors.items()))
            return _error(str(messages[0]), status.HTTP_400_BAD_REQUEST, field)
        try:
            result = build_plan(
                serializer.validated_data["start"],
                serializer.validated_data["finish"],
                stop_penalty=serializer.validated_data.get("stop_penalty"),
            )
        except PlanningError as exc:
            return _error(str(exc), exc.status, exc.field)
        location = reverse("route-plan-detail", kwargs={"plan_id": result.plan.pk}, request=request)
        return Response(
            _with_absolute_map_url(request, result.payload),
            status=status.HTTP_201_CREATED,
            headers={"Location": location},
        )


class RoutePlanDetailView(APIView):
    def get(self, request, plan_id):
        plan = get_object_or_404(RoutePlan, pk=plan_id)
        return Response(_with_absolute_map_url(request, with_map(plan.result, plan.route_coordinates)))


def route_plan_map(request, plan_id):
    """Interactive Leaflet map of a stored plan (no routing API call)."""
    plan = get_object_or_404(RoutePlan, pk=plan_id)
    return render(
        request,
        "fuel/map.html",
        {"plan": plan.result, "route_coordinates": plan.route_coordinates},
    )
