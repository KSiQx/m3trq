import uuid
from django.db import models
from django.conf import settings
from django.core.validators import MinValueValidator, MaxValueValidator


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
            models.UniqueConstraint(
                fields=['url', 'status'],
                name='unique_url_status'
            ),
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


class Article(models.Model):
    """News articles with sentiment analysis"""
    LANGUAGE_CHOICES = [
        ('zh_cn', 'Chinese Simplified'),
        ('zh_tw', 'Chinese Traditional'),
        ('en', 'English'),
        ('ru', 'Russian'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    search_themes = models.CharField(max_length=255, blank=True)
    news_provider = models.CharField(max_length=255, db_index=True)
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
    entities = models.JSONField(default=dict, blank=True)
    geotags = models.JSONField(default=dict, blank=True)
    language = models.CharField(
        max_length=10,
        choices=LANGUAGE_CHOICES,
        db_index=True
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
        ('weekly', 'Weekly'),
        ('monthly', 'Monthly'),
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
