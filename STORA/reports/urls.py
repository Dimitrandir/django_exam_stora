from django.urls import path

from STORA.reports.views import (
    ReportsDashboardView,
    SalesReportView,
    DeliveriesReportView,
    StockAsOfDateReportView,
    ExpiringProductsReportView,
    SalesQuantityReportView,
    AIReportListView,
    AIReportCreateView,
    AIReportDetailView,
    AIReportRegenerateView,
    AIReportDeleteView,
)

urlpatterns = [
    path('', ReportsDashboardView.as_view(), name='reports_dashboard'),
    path('sales/', SalesReportView.as_view(), name='sales_report'),
    path('deliveries/', DeliveriesReportView.as_view(), name='deliveries_report'),
    path('stock-as-of/', StockAsOfDateReportView.as_view(), name='stock_as_of_report'),
    path('expiring/', ExpiringProductsReportView.as_view(), name='expiring_report'),
    path('sales-quantity/', SalesQuantityReportView.as_view(), name='sales_quantity_report'),
    path('ai/', AIReportListView.as_view(), name='ai_report_list'),
    path('ai/new/', AIReportCreateView.as_view(), name='ai_report_create'),
    path('ai/<int:pk>/', AIReportDetailView.as_view(), name='ai_report_detail'),
    path('ai/<int:pk>/regenerate/', AIReportRegenerateView.as_view(), name='ai_report_regenerate'),
    path('ai/<int:pk>/delete/', AIReportDeleteView.as_view(), name='ai_report_delete'),
]