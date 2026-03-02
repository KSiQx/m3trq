"""
MetrQ Geopolitical Intelligence Extraction Models (Layer A-E)
Extract rich geopolitical intelligence from news articles.
"""
import uuid
from django.db import models
from django.core.validators import MinValueValidator, MaxValueValidator

from .choices import (
    EVENT_TYPE_CHOICES,
    ACTOR_TYPE_CHOICES,
    LOCATION_ROLE_CHOICES,
    STRATEGIC_SIGNIFICANCE_CHOICES,
    CONFLICT_ROLE_CHOICES,
    RELATIONSHIP_TYPE_CHOICES,
    CONFIDENCE_CHOICES,
    DIRECTION_CHOICES,
    INTENSITY_CHOICES,
    VERIFICATION_STATUS_CHOICES,
)


class ArticleEvent(models.Model):
    """
    Layer A: Event type classification for articles.
    One article can have multiple event types.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    article = models.ForeignKey(
        'core.Article',
        on_delete=models.CASCADE,
        related_name='events',
        db_index=True
    )
    event_type = models.CharField(
        max_length=50,
        choices=EVENT_TYPE_CHOICES,
        db_index=True
    )
    confidence = models.FloatField(
        null=True,
        blank=True,
        validators=[MinValueValidator(0), MaxValueValidator(1)],
        help_text="Confidence score 0-1"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'core_articleevent'
        indexes = [
            models.Index(fields=['article', 'event_type']),
            models.Index(fields=['event_type', 'created_at']),
        ]
        verbose_name = 'Article Event'
        verbose_name_plural = 'Article Events'

    def __str__(self):
        return f"{self.article.id[:8]}... - {self.get_event_type_display()}"


class ArticleLocation(models.Model):
    """
    Layer B: Geographic locations mentioned in articles.
    Includes hierarchy, coordinates, and strategic significance.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    article = models.ForeignKey(
        'core.Article',
        on_delete=models.CASCADE,
        related_name='locations',
        db_index=True
    )
    name = models.CharField(max_length=255, db_index=True)
    hierarchy = models.JSONField(
        default=list,
        help_text='List of strings: ["Continent", "Country", "Region", "City"]'
    )
    coordinates = models.JSONField(
        null=True,
        blank=True,
        help_text='{"lat": float, "lng": float} or null'
    )
    role_in_event = models.CharField(
        max_length=50,
        choices=LOCATION_ROLE_CHOICES,
        default='reference'
    )
    strategic_significance = models.CharField(
        max_length=20,
        choices=STRATEGIC_SIGNIFICANCE_CHOICES,
        default='none'
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'core_articlelocation'
        indexes = [
            models.Index(fields=['article', 'name']),
            models.Index(fields=['name', 'strategic_significance']),
            models.Index(fields=['article', 'role_in_event']),
        ]
        verbose_name = 'Article Location'
        verbose_name_plural = 'Article Locations'

    def __str__(self):
        return f"{self.name} ({self.get_role_in_event_display()})"

    def get_full_hierarchy(self):
        """Return hierarchy as a string."""
        return ' > '.join(self.hierarchy) if self.hierarchy else 'Unknown'


class ArticleActor(models.Model):
    """
    Layer C: Actors (entities) mentioned in articles.
    Includes type, roles, affiliations, and beneficial interests.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    article = models.ForeignKey(
        'core.Article',
        on_delete=models.CASCADE,
        related_name='actors',
        db_index=True
    )
    name = models.CharField(max_length=255, db_index=True)
    type = models.CharField(
        max_length=50,
        choices=ACTOR_TYPE_CHOICES,
        default='other'
    )
    type_detail = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text="Detailed type description"
    )
    roles = models.JSONField(
        default=list,
        help_text='List of roles: ["agent", "beneficiary", "perpetrator"]'
    )
    affiliations = models.JSONField(
        default=list,
        help_text='List of affiliation objects with: with, type, evidence, confidence'
    )
    beneficial_interests = models.JSONField(
        default=list,
        null=True,
        blank=True,
        help_text='List of interest objects with: type, description, evidence, confidence'
    )
    conflict_role = models.CharField(
        max_length=50,
        choices=CONFLICT_ROLE_CHOICES,
        blank=True,
        null=True
    )
    power_index = models.FloatField(
        null=True,
        blank=True,
        validators=[MinValueValidator(0), MaxValueValidator(1)],
        help_text="Power/influence score 0-1"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'core_articleactor'
        indexes = [
            models.Index(fields=['article', 'name']),
            models.Index(fields=['article', 'type']),
            models.Index(fields=['name', 'type']),
            models.Index(fields=['conflict_role']),
        ]
        verbose_name = 'Article Actor'
        verbose_name_plural = 'Article Actors'

    def __str__(self):
        return f"{self.name} ({self.get_type_display()})"


class ArticleRelationship(models.Model):
    """
    Layer D: Relationships between actors in articles.
    Directed or bidirectional relationships with evidence.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    article = models.ForeignKey(
        'core.Article',
        on_delete=models.CASCADE,
        related_name='relationships',
        db_index=True
    )
    source = models.CharField(max_length=255, db_index=True)
    target = models.CharField(max_length=255, db_index=True)
    relationship_type = models.CharField(
        max_length=50,
        choices=RELATIONSHIP_TYPE_CHOICES,
        db_index=True
    )
    evidence = models.TextField(help_text="Text evidence from article")
    confidence = models.CharField(
        max_length=10,
        choices=CONFIDENCE_CHOICES,
        default='medium'
    )
    direction = models.CharField(
        max_length=20,
        choices=DIRECTION_CHOICES,
        default='unidirectional'
    )
    intensity = models.CharField(
        max_length=20,
        choices=INTENSITY_CHOICES,
        blank=True,
        null=True
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'core_articlerelationship'
        indexes = [
            models.Index(fields=['article', 'relationship_type']),
            models.Index(fields=['source', 'target']),
            models.Index(fields=['relationship_type', 'confidence']),
        ]
        verbose_name = 'Article Relationship'
        verbose_name_plural = 'Article Relationships'

    def __str__(self):
        dir_arrow = "↔" if self.direction == 'bidirectional' else "→"
        return f"{self.source} {dir_arrow} {self.target} ({self.get_relationship_type_display()})"


class ArticleClaim(models.Model):
    """
    Metadata claims: Statements from articles with verification status.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    article = models.ForeignKey(
        'core.Article',
        on_delete=models.CASCADE,
        related_name='claims',
        db_index=True
    )
    text = models.TextField(help_text="The claim text")
    verification_status = models.CharField(
        max_length=20,
        choices=VERIFICATION_STATUS_CHOICES,
        default='unverified'
    )
    confidence = models.FloatField(
        validators=[MinValueValidator(0), MaxValueValidator(1)],
        help_text="Confidence score 0-1"
    )
    supporting_evidence = models.TextField(blank=True, null=True)
    contradicting_evidence = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'core_articleclaim'
        indexes = [
            models.Index(fields=['article', 'verification_status']),
            models.Index(fields=['verification_status', 'confidence']),
        ]
        verbose_name = 'Article Claim'
        verbose_name_plural = 'Article Claims'

    def __str__(self):
        text_short = self.text[:50] + "..." if len(self.text) > 50 else self.text
        return f"{text_short} ({self.get_verification_status_display()})"
