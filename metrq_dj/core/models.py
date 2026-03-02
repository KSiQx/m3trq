import uuid
import hashlib
import logging
import secrets
from datetime import timedelta

from django.db import models, transaction
from django.conf import settings
from django.core.validators import MinValueValidator, MaxValueValidator
from django.utils import timezone

from .choices import CONFLICT_INTENSITY_CHOICES


logger = logging.getLogger(__name__)


class RateLimit(models.Model):
    """Tracks API usage per user per day"""
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        db_index=True
    )
    request_date = models.DateField(db_index=True)
    request_count = models.IntegerField(default=0)

    class Meta:
        unique_together = ['user', 'request_date']
        indexes = [
            models.Index(fields=['request_date']),
            models.Index(fields=['user', 'request_date']),
        ]

    def __str__(self):
        return f"{self.user.username} - {self.request_date}: {self.request_count}"


class Job(models.Model):
    """Job queue for batch processing"""
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('processing', 'Processing'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
        ('timeout', 'Timeout'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    url = models.URLField(max_length=2000, db_index=True)
    priority = models.IntegerField(
        default=1,
        validators=[MinValueValidator(0), MaxValueValidator(10)]
    )
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='pending',
        db_index=True
    )
    retries = models.IntegerField(
        default=0,
        validators=[MaxValueValidator(5)]
    )
    error_message = models.TextField(null=True, blank=True)
    locked_by = models.CharField(max_length=255, null=True, blank=True)
    locked_at = models.DateTimeField(null=True, blank=True, db_index=True)
    enqueued_at = models.DateTimeField(auto_now_add=True, db_index=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                check=models.Q(priority__gte=0, priority__lte=10),
                name='valid_priority_range'
            ),
            models.CheckConstraint(
                check=models.Q(retries__lte=5),
                name='max_retries_limit'
            ),
        ]
        indexes = [
            models.Index(fields=['status', '-priority', 'enqueued_at']),
            models.Index(
                fields=['locked_at'],
                condition=models.Q(locked_at__isnull=False),
                name='idx_jobs_locked_at_notnull'
            ),
        ]

    def __str__(self):
        return f"Job {self.id} - {self.status}"

    @classmethod
    def create_or_get_pending(cls, url: str, priority: int = 1) -> 'Job':
        """
        ATOMIC pending job creation with race-condition protection.
        SQLite/PostgreSQL ready
        """
        with transaction.atomic():
            # Row lock + check
            pending_job = cls.objects.select_for_update().filter(
                url=url,
                status='pending'
            ).first()

            if pending_job:
                logger.info(f"Found existing pending job: {pending_job.id}")
                return pending_job

            # Only one process will create job
            job = cls.objects.create(
                url=url,
                priority=priority,
                status='pending'
            )
            logger.info(f"Created NEW pending job: {job.id}")
            return job


class Article(models.Model):
    """News articles with sentiment analysis and multi-stage processing status"""
    LANGUAGE_CHOICES = [
        ('zh_cn', 'Chinese Simplified'),
        ('zh_tw', 'Chinese Traditional'),
        ('en', 'English'),
        ('ru', 'Russian'),
    ]

    STATUS_CHOICES = [
        ('new', 'New'),
        ('analyzing', 'Analyzing'),
        ('analyzed', 'Analyzed'),
        ('translating', 'Translating'),
        ('translated', 'Translated'),
        ('ready', 'Ready'),
        ('skipped', 'Skipped'),
        ('failed', 'Failed'),
    ]

    # Source category choices (Unified System IDs (Slugs)) from config/sources.json
    SOURCE_CATEGORY_CHOICES = [
        ('politics_domestic', 'Domestic Politics'),
        ('politics_global', 'Global Politics'),
        ('security_military', 'Military Security'),
        ('security_civil', 'Civil Security'),
        ('economy_macro', 'Macro Economy'),
        ('economy_markets', 'Markets'),
        ('economy_business', 'Business'),
        ('energy_security', 'Energy Security'),
        ('tech_strategic', 'Strategic Technology'),
        ('resource_environment', 'Resources & Environment'),
        ('health_science', 'Health & Science'),
        ('regional_focus', 'Regional Focus'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    search_themes = models.CharField(max_length=255, blank=True)
    news_provider = models.CharField(max_length=255, db_index=True)
    source_category = models.CharField(
        max_length=30,
        choices=SOURCE_CATEGORY_CHOICES,
        default='politics_global',
        db_index=True,
        help_text="Category from JSON source configuration (e.g., politics_domestic, economy_macro)"
    )
    published_at = models.DateField(db_index=True)
    title_origin = models.TextField()
    title_translated = models.TextField(null=True, blank=True)
    text_origin = models.TextField(null=True, blank=True)
    text_translated = models.TextField(null=True, blank=True)
    url = models.URLField(max_length=2000, unique=True, db_index=True)
    sentiment = models.FloatField(
        null=True,
        blank=True,
        validators=[MinValueValidator(-1), MaxValueValidator(1)]
    )
    bias = models.FloatField(
        null=True,
        blank=True,
        validators=[MinValueValidator(0), MaxValueValidator(1)]
    )
    importance_score = models.FloatField(
        null=True,
        blank=True,
        validators=[MinValueValidator(0), MaxValueValidator(1)],
        help_text="Confidence score from importance classification (0-1)"
    )
    entities = models.JSONField(default=dict, blank=True)
    geotags = models.JSONField(default=dict, blank=True)
    language = models.CharField(
        max_length=10,
        choices=LANGUAGE_CHOICES,
        db_index=True
    )
    # Layer E metadata
    conflict_intensity = models.CharField(
        max_length=20,
        choices=CONFLICT_INTENSITY_CHOICES,
        blank=True,
        null=True,
        db_index=True,
        help_text="Overall conflict intensity (low/medium/high)"
    )

    # To store the complete structure Layer E (JSON)
    conflict_dynamics = models.JSONField(
        default=dict,
        blank=True,
        help_text="Detailed Layer E conflict dynamics data"
    )
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='new',
        db_index=True,
        help_text="Current stage in the processing pipeline"
    )
    scraped_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    worker_id = models.CharField(max_length=255, null=True, blank=True)
    version = models.IntegerField(default=1)

    class Meta:
        indexes = [
            models.Index(fields=['-published_at']),
            models.Index(fields=['-scraped_at']),
            models.Index(fields=['language', '-published_at']),
            models.Index(fields=['language', '-scraped_at', 'sentiment']),
            models.Index(fields=['status', '-scraped_at']),
            models.Index(fields=['source_category', '-scraped_at']),
            models.Index(fields=['status', 'source_category']),
            models.Index(fields=['status', 'importance_score']),
            models.Index(fields=['importance_score', '-scraped_at']),
        ]
        constraints = [
            models.CheckConstraint(
                check=models.Q(sentiment__gte=-1, sentiment__lte=1),
                name='valid_sentiment_range'
            ),
            models.CheckConstraint(
                check=models.Q(bias__gte=0, bias__lte=1),
                name='valid_bias_range'
            ),
        ]

    def __str__(self):
        return f"{self.title_origin[:50]}... ({self.language})"


class Report(models.Model):
    """Generated reports storage"""
    TYPE_CHOICES = [
        ('daily', 'Daily'),
        ('weekly', 'Weekly'),
        ('monthly', 'Monthly'),
        ('quarterly', 'Quarterly'),
        ('semi_annual', 'Semi_annual'),
        ('annual', 'Annual')
    ]
    FORMAT_CHOICES = [
        ('pdf', 'PDF'),
        ('excel', 'Excel'),
    ]
    STATUS_CHOICES = [
        ('queued', 'Queued'),
        ('processing', 'Processing'),
        ('done', 'Done'),
        ('failed', 'Failed'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        db_index=True
    )
    type = models.CharField(max_length=20, choices=TYPE_CHOICES)
    format = models.CharField(max_length=10, choices=FORMAT_CHOICES)
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='queued',
        db_index=True
    )
    file_blob = models.BinaryField(null=True, blank=True)
    error_message = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    worker_id = models.CharField(max_length=255, null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=['user', '-created_at']),
            models.Index(
                fields=['status'],
                condition=models.Q(status__in=['queued', 'processing']),
                name='idx_reports_active_status'
            ),
        ]

    def __str__(self):
        return f"Report {self.id} - {self.type} ({self.status})"


class ProviderLog(models.Model):
    """Provider system logs with TTL"""
    LEVEL_CHOICES = [
        ('debug', 'Debug'),
        ('info', 'Info'),
        ('warning', 'Warning'),
        ('error', 'Error'),
        ('critical', 'Critical'),
    ]

    id = models.AutoField(primary_key=True)
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)
    level = models.CharField(max_length=10, choices=LEVEL_CHOICES, db_index=True)
    message = models.TextField()
    data = models.JSONField(default=dict, blank=True)
    worker_id = models.CharField(max_length=255, null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=['timestamp']),
            models.Index(fields=['level', 'timestamp']),
        ]

    def __str__(self):
        return f"{self.timestamp} - {self.level}: {self.message[:50]}"


class ProviderApiKey(models.Model):
    """ Hashed storage of provider API keys """
    name = models.CharField(max_length=100)
    key_hash = models.CharField(max_length=64, unique=True)  # SHA-256
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    request_count = models.IntegerField(default=0)
    last_ip = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=['is_active', 'expires_at']),
        ]

    def __str__(self):
        return f"{self.name} ({'active' if self.is_active else 'inactive'})"

    @classmethod
    def create_key(cls, name: str) -> str:
        """ Create a new API key """
        raw_key = secrets.token_urlsafe(32)
        key_hash = hashlib.sha256(raw_key.encode()).hexdigest()

        cls.objects.create(
            name=name,
            key_hash=key_hash,
            expires_at=timezone.now() + timedelta(days=90)
        )

        return raw_key

    @classmethod
    def validate_key(cls, api_key: str) -> bool:
        """ Check API key """
        if not api_key:
            return False

        key_hash = hashlib.sha256(api_key.encode()).hexdigest()

        try:
            key_record = cls.objects.get(
                key_hash=key_hash,
                is_active=True
            )

            # Check expiration date
            if key_record.expires_at and key_record.expires_at < timezone.now():
                key_record.is_active = False
                key_record.save()
                return False

            # Update metrics
            key_record.last_used_at = timezone.now()
            key_record.request_count += 1
            key_record.save()

            return True

        except cls.DoesNotExist:
            return False


class Announcement(models.Model):
    """
    Announcement banner for dashboard communication.
    Displays active announcements to authenticated users.
    """
    title = models.CharField(
        max_length=200,
        blank=True,
        help_text="Internal name for admin reference"
    )
    message = models.TextField(
        help_text="Banner text to display (HTML allowed for basic formatting)"
    )
    link_url = models.URLField(
        blank=True,
        null=True,
        help_text="Optional URL to open when banner is clicked"
    )
    is_active = models.BooleanField(
        default=True,
        help_text="Enable/disable this announcement"
    )
    start_date = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When to start showing (empty = immediately)"
    )
    end_date = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When to stop showing (empty = indefinite)"
    )
    priority = models.IntegerField(
        default=0,
        help_text="Higher priority announcements shown first"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-priority', '-created_at']
        verbose_name = "Announcement"
        verbose_name_plural = "Announcements"

    def __str__(self):
        return self.title or f"Announcement #{self.id}"

    def is_currently_active(self):
        """Check if announcement should be displayed now."""
        if not self.is_active:
            return False

        now = timezone.now()

        # Check start_date (if set, must be <= now)
        if self.start_date is not None and now < self.start_date:
            return False

        # Check end_date (if set, must be >= now)
        if self.end_date is not None and now > self.end_date:
            return False

        return True
