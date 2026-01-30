import uuid
from django.db import models
from django.contrib.auth.models import User
from django.db.models.signals import post_save
from django.dispatch import receiver


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
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=['tier']),
            models.Index(fields=['reports_used']),
        ]

    def __str__(self):
        return f"{self.user.username} - {self.tier}"

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


@receiver(post_save, sender=User)
def create_user_profile(sender, instance, created, **kwargs):
    """Auto-create profile when user is created"""
    if created:
        Profile.objects.create(user=instance)


@receiver(post_save, sender=User)
def save_user_profile(sender, instance, **kwargs):
    """Save profile when user is saved"""
    instance.profile.save()
    