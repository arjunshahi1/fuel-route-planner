from django.urls import path

from . import views

urlpatterns = [
    path("", views.ApiRootView.as_view(), name="api-root"),
    path("route-plans/", views.RoutePlanView.as_view(), name="route-plan"),
    path("route-plans/<uuid:plan_id>/", views.RoutePlanDetailView.as_view(), name="route-plan-detail"),
    path("route-plans/<uuid:plan_id>/map/", views.route_plan_map, name="route-plan-map"),
]
