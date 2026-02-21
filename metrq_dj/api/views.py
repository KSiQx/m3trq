from django.views.generic import TemplateView
from django.conf import settings

class DashboardView(TemplateView):
    template_name = 'dashboard/dashboard.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # Passing update intervals to the template
        context['refresh_intervals'] = settings.DASHBOARD_REFRESH_INTERVALS
        return context
