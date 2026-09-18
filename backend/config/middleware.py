from django.contrib import admin
from django.http import HttpResponseRedirect


class StaffOnlyAdminMiddleware:

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        if request.resolver_match.app_name == "admin" and not admin.site.has_permission(request):
            return HttpResponseRedirect("/steins-gate")
