from django.urls import path
from .views import HealthAPIView, PlanRouteAPIView, PlanRouteMapAPIView

urlpatterns = [
    path('api/health/', HealthAPIView.as_view(), name='health'),
    path('api/plan-route/map/', PlanRouteMapAPIView.as_view(), name='plan-route-map'),
    path('api/plan-route/', PlanRouteAPIView.as_view(), name='plan-route'),
]
