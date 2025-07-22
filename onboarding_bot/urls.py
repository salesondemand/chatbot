from django.contrib import admin
from django.urls import path, include

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', include('onboarding.urls')),
        # 👈 This loads URLs from your app
]
