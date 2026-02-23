import uuid
from django.db import models
from django.contrib.auth.models import User
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.conf import settings


class Organization(models.Model):
    """Enterprise organization for automatic tier assignment"""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    enterprise_tag = models.CharField(max_length=100, unique=True, db_index=True)
    max_licenses = models.PositiveIntegerField(default=10)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=['enterprise_tag', 'is_active']),
        ]

    def __str__(self):
        return f"{self.name} ({self.enterprise_tag})"

    def save(self, *args, **kwargs):
        # Automatically convert to lowercase when saving
        self.enterprise_tag = self.enterprise_tag.lower().strip()
        super().save(*args, **kwargs)

    @property
    def display_tag(self):
        # For display - original register
        return self.enterprise_tag.title()

    @property
    def used_licenses(self):
        """Count active profiles associated with this organization"""
        return self.profiles.filter(user__is_active=True).count()  # type: ignore

    @property
    def available_licenses(self):
        """Calculate remaining licenses"""
        return max(0, self.max_licenses - self.used_licenses)

    def has_available_license(self):
        """Check if organization can accept new users"""
        return self.is_active and self.available_licenses > 0


class Profile(models.Model):
    TIER_CHOICES = [
        ('free', 'Free'),
        ('pro', 'Pro'),
        ('enterprise', 'Enterprise'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name='profile'
    )
    tier = models.CharField(
        max_length=10,
        choices=TIER_CHOICES,
        default='free',
        db_index=True
    )
    max_reports = models.PositiveSmallIntegerField(default=1)
    reports_used = models.PositiveSmallIntegerField(default=0, db_index=True)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='profiles'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=['tier']),
            models.Index(fields=['reports_used']),
            models.Index(fields=['organization', 'tier']),
        ]

    def __str__(self):
        return f"{self.user.username} - {self.tier}"

    def save(self, *args, **kwargs):
        # Set max_reports based on the current tariff
        self.max_reports = settings.TIER_REPORT_LIMITS.get(self.tier, 1)
        super().save(*args, **kwargs)

    @property
    def reports_remaining(self):
        """Calculate remaining reports for the user"""
        if self.tier == 'enterprise':
            return float('inf')
        return max(0, self.max_reports - self.reports_used)

    def can_generate_report(self):
        """Check if user can generate a new report"""
        if self.tier == 'enterprise':
            return True
        return self.reports_used < self.max_reports
