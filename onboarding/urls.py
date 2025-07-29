from django.urls import path
from . import views

urlpatterns = [
    path('webhook/', views.meta_webhook, name='webhook'),
    path('upload_excel/', views.upload_excel, name='upload_excel'),
    path('get_escalated/', views.get_escalated, name='get_escalated'),
    path('get_chat_history/', views.get_chat_history, name='get_chat_history'),
    path('send_admin_reply/', views.send_admin_reply, name='send_admin_reply'),
    path('resume_bot/', views.resume_bot, name='resume_bot'),
    path('get_all_chats/', views.get_all_chats, name='get_all_chats'),
    path('get_report_stats/', views.get_report_stats, name='get_report_stats'),


]
