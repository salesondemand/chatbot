from django.db import models

class Candidate(models.Model):
    name = models.CharField(max_length=100)
    surname = models.CharField(max_length=100)
    phone_number = models.CharField(max_length=20, unique=True)
    status = models.CharField(max_length=20, default='sent')
    history = models.JSONField(null=True, blank=True)
    last_updated = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name or self.phone_number
