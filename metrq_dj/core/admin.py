"""
Django Admin enhancements for Provider Control Panel.
Read-optimized interfaces for operational monitoring.
"""
import json
from datetime import timedelta
from django.contrib import admin
from django.utils import timezone
from django.utils.html import format_html
from django.db import models, transaction
from django import forms

from .models import Job, ProviderLog, ProviderApiKey, Article, RateLimit, Report
from .models import Announcement


# ============================================================================
# ANNOUNCEMENT ADMIN
# ============================================================================

@admin.register(Announcement)
class AnnouncementAdmin(admin.ModelAdmin):
    list_display = [
        'title',
        'message_short',
        'is_active',
        'priority',
        'start_date',
        'end_date',
        'created_at'
    ]
    list_filter = [
        'is_active',
        ('start_date', admin.DateFieldListFilter),
        ('end_date', admin.DateFieldListFilter),
        'priority'
    ]
    search_fields = ['title', 'message']
    readonly_fields = ['created_at', 'updated_at']
    fieldsets = (
        ('Content', {
            'fields': ('title', 'message', 'link_url')
        }),
        ('Scheduling', {
            'fields': ('is_active', 'start_date', 'end_date', 'priority'),
            'description': 'Set start/end dates to schedule announcements. Leave blank for immediate/indefinite display.'
        }),
        ('Metadata', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        })
    )

    def message_short(self, obj):
        if len(obj.message) > 50:
            return obj.message[:50] + '...'
        return obj.message
    message_short.short_description = 'Message Preview'


# ============================================================================
# JOB ADMIN
# ============================================================================

@admin.register(Job)
class JobAdmin(admin.ModelAdmin):
    list_display = [
        'id_short',
        'url_short',
        'status_badge',
        'priority',
        'locked_by_short',
        'enqueued_at',
        'age'
    ]
    list_filter = [
        'status',
        'priority',
        ('enqueued_at', admin.DateFieldListFilter),
        'locked_by'
    ]
    search_fields = ['url', 'locked_by', 'error_message']
    readonly_fields = [
        'id',
        'enqueued_at',
        'locked_at',
        'completed_at',
        'retries'
    ]
    list_per_page = 50
    actions = ['reset_to_pending', 'mark_failed', 'increase_priority']

    fieldsets = (
        ('Status', {
            'fields': ('status', 'priority', 'retries')
        }),
        ('Content', {
            'fields': ('url',)
        }),
        ('Locking', {
            'fields': ('locked_by', 'locked_at'),
            'classes': ('collapse',)
        }),
        ('Timing', {
            'fields': ('enqueued_at', 'completed_at'),
            'classes': ('collapse',)
        }),
        ('Error', {
            'fields': ('error_message',),
            'classes': ('collapse',)
        })
    )

    def id_short(self, obj):
        return str(obj.id)[:8]

    id_short.short_description = 'ID'

    def url_short(self, obj):
        if len(obj.url) > 50:
            return obj.url[:50] + '...'
        return obj.url

    url_short.short_description = 'URL'

    def locked_by_short(self, obj):
        if obj.locked_by:
            if len(obj.locked_by) > 20:
                return obj.locked_by[:20] + '...'
            return obj.locked_by
        return '-'

    locked_by_short.short_description = 'Worker'

    def status_badge(self, obj):
        colors = {
            'pending': 'gray',
            'processing': 'orange',
            'completed': 'green',
            'failed': 'red',
            'timeout': 'darkred'
        }
        color = colors.get(obj.status, 'black')
        return format_html(
            '<span style="background-color: {color}; color: white; padding: 2px 6px; border-radius: 3px;">{status}</span>',
            color=color,
            status=obj.status
        )

    status_badge.short_description = 'Status'

    def age(self, obj):
        """Show how old the job is"""
        age = timezone.now() - obj.enqueued_at
        if age < timedelta(minutes=1):
            return f"{age.seconds}s"
        elif age < timedelta(hours=1):
            return f"{age.seconds // 60}m"
        elif age < timedelta(days=1):
            return f"{age.seconds // 3600}h"
        else:
            return f"{age.days}d"

    age.short_description = 'Age'


    @admin.action(description='Reset to NEW pending (safe)')
    def reset_to_pending_safe(self, request, queryset):
        """Creates NEW pending tasks without duplicates"""
        created_count = 0
        with transaction.atomic():
            for job in queryset.filter(status__in=['processing', 'failed', 'timeout']):
                # Atomically creates/returns pending
                new_pending = Job.create_or_get_pending(job.url, job.priority)
                if str(new_pending.id) != str(job.id):
                    created_count += 1
                    self.log_change(request, new_pending, f"Created from reset")

        self.message_user(request, f'Created {created_count} new pending jobs')
    # @admin.action(description='Reset to pending (safe)')
    # def reset_to_pending_safe(self, request, queryset):
    #     """Safe reset: use create_or_get_pending logic"""
    #     reset_count = 0
    #     for job in queryset.filter(status__in=['processing', 'failed', 'timeout']):
    #         # Creates a new pending task if there is no active one.
    #         new_job = Job.create_or_get_pending(job.url, job.priority)
    #         if new_job != job:
    #             reset_count += 1
    #     self.message_user(request, f'Created {reset_count} new pending jobs')
    # @admin.action(description='Reset selected jobs to pending')
    # def reset_to_pending(self, request, queryset):
    #     updated = queryset.update(
    #         status='pending',
    #         locked_by=None,
    #         locked_at=None
    #     )
    #     self.message_user(request, f'{updated} jobs reset to pending')

    @admin.action(description='Mark selected jobs as failed')
    def mark_failed(self, request, queryset):
        updated = queryset.update(status='failed')
        self.message_user(request, f'{updated} jobs marked as failed')

    @admin.action(description='Increase priority by 1')
    def increase_priority(self, request, queryset):
        for job in queryset:
            job.priority = min(10, job.priority + 1)
            job.save()
        self.message_user(request, f'Priority increased for {queryset.count()} jobs')


# ============================================================================
# PROVIDER LOG ADMIN
# ============================================================================

@admin.register(ProviderLog)
class ProviderLogAdmin(admin.ModelAdmin):
    list_display = [
        'timestamp',
        'level_badge',
        'message_short',
        'worker_id',
        'has_data'
    ]
    list_filter = [
        'level',
        ('timestamp', admin.DateFieldListFilter),
        'worker_id'
    ]
    search_fields = ['message', 'data']
    readonly_fields = ['timestamp', 'data_formatted']
    list_per_page = 100
    actions = ['cleanse_old_logs']
    date_hierarchy = 'timestamp'

    def level_badge(self, obj):
        colors = {
            'debug': 'gray',
            'info': 'blue',
            'warning': 'orange',
            'error': 'red',
            'critical': 'darkred'
        }
        color = colors.get(obj.level, 'black')
        return format_html(
            '<span style="color: {}; font-weight: bold;">{}</span>',
            color,
            obj.level.upper()
        )

    level_badge.short_description = 'Level'

    def message_short(self, obj):
        if len(obj.message) > 60:
            return obj.message[:60] + '...'
        return obj.message

    message_short.short_description = 'Message'

    def has_data(self, obj):
        return bool(obj.data)

    has_data.boolean = True
    has_data.short_description = 'Data?'

    def data_formatted(self, obj):
        """Pretty print JSON data"""
        if not obj.data:
            return '-'
        try:
            if isinstance(obj.data, str):
                data = json.loads(obj.data)
            else:
                data = obj.data
            return format_html('<pre>{}</pre>', json.dumps(data, indent=2))
        except:
            return obj.data

    data_formatted.short_description = 'Data (JSON)'

    @admin.action(description='Cleanse logs older than 7 days')
    def cleanse_old_logs(self, request, queryset):
        """Admin action to cleanse old logs"""
        cutoff = timezone.now() - timedelta(days=7)
        deleted, _ = ProviderLog.objects.filter(timestamp__lt=cutoff).delete()
        self.message_user(request, f'{deleted} old log entries deleted')


# ============================================================================
# PROVIDER API KEY ADMIN
# ============================================================================

@admin.register(ProviderApiKey)
class ProviderApiKeyAdmin(admin.ModelAdmin):
    list_display = [
        'name',
        'created_at',
        'expires_at',
        'is_active',
        'is_expired',
        'last_used_at',
        'requests_today'
    ]
    list_filter = [
        'is_active',
        ('expires_at', admin.DateFieldListFilter),
        ('created_at', admin.DateFieldListFilter)
    ]
    readonly_fields = [
        'key_hash',
        'created_at',
        'last_used_at',
        'request_count'
    ]
    actions = ['deactivate_selected', 'extend_expiry_90_days']

    def is_expired(self, obj):
        if obj.expires_at and obj.expires_at < timezone.now():
            return True
        return False

    is_expired.boolean = True
    is_expired.short_description = 'Expired?'

    def requests_today(self, obj):
        """Show today's request count if tracked"""
        return getattr(obj, 'request_count', 0)

    requests_today.short_description = 'Requests'

    @admin.action(description='Deactivate selected keys')
    def deactivate_selected(self, request, queryset):
        queryset.update(is_active=False)
        self.message_user(request, f'{queryset.count()} keys deactivated')

    @admin.action(description='Extend expiry by 90 days')
    def extend_expiry_90_days(self, request, queryset):
        for key in queryset:
            if key.expires_at:
                key.expires_at = key.expires_at + timedelta(days=90)
            else:
                key.expires_at = timezone.now() + timedelta(days=90)
            key.save()
        self.message_user(request, f'{queryset.count()} keys extended')


# ============================================================================
# ARTICLE ADMIN
# ============================================================================

@admin.register(Article)
class ArticleAdmin(admin.ModelAdmin):
    list_display = [
        'title_short',
        'language',
        'news_provider',
        'sentiment_badge',
        'scraped_at'
    ]
    list_filter = [
        'language',
        'news_provider',
        ('scraped_at', admin.DateFieldListFilter)
    ]
    search_fields = [
        'title_origin',
        'title_translated',
        'text_origin',
        'entities'
    ]
    readonly_fields = ['id', 'scraped_at', 'updated_at']
    list_per_page = 50

    def title_short(self, obj):
        title = obj.title_translated or obj.title_origin
        if len(title) > 50:
            return title[:50] + '...'
        return title

    title_short.short_description = 'Title'

    def sentiment_badge(self, obj):
        if obj.sentiment is None:
            return '-'
        color = 'green' if obj.sentiment > 0.3 else 'red' if obj.sentiment < -0.3 else 'gray'
        return format_html(
            '<span style="color: {};">{:.2f}</span>',
            color,
            obj.sentiment
        )

    sentiment_badge.short_description = 'Sentiment'


# ============================================================================
# REPORT ADMIN
# ============================================================================

@admin.register(Report)
class ReportAdmin(admin.ModelAdmin):
    list_display = [
        'id_short',
        'user',
        'type',
        'format',
        'status_badge',
        'created_at'
    ]
    list_filter = ['status', 'type', 'format']
    readonly_fields = ['id', 'created_at', 'completed_at']

    def id_short(self, obj):
        return str(obj.id)[:8]

    id_short.short_description = 'ID'

    def status_badge(self, obj):
        colors = {
            'queued': 'gray',
            'processing': 'orange',
            'done': 'green',
            'failed': 'red'
        }
        color = colors.get(obj.status, 'black')
        return format_html(
            '<span style="background-color: {}; color: white; padding: 2px 6px; border-radius: 3px;">{}</span>',
            color,
            obj.status
        )

    status_badge.short_description = 'Status'


# ============================================================================
# RATE LIMIT ADMIN (Read-only for debugging)
# ============================================================================

@admin.register(RateLimit)
class RateLimitAdmin(admin.ModelAdmin):
    list_display = ['user', 'request_date', 'request_count']
    list_filter = ['request_date']
    readonly_fields = ['user', 'request_date']  # Prevent manual manipulation
    actions = ['reset_selected_counts']

    @admin.action(description='Reset selected rate limit counts')
    def reset_selected_counts(self, request, queryset):
        queryset.update(request_count=0)
        self.message_user(request, f'{queryset.count()} rate limits reset')
