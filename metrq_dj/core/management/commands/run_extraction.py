"""
Management command to manually trigger Layer A-E extraction.
Usage: python manage.py run_extraction --article <uuid>
"""
import json
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.models import Article
from core.tasks.extraction import extract_layer_a_e


class Command(BaseCommand):
    help = 'Run Layer A-E extraction on a specific article or batch'

    def add_arguments(self, parser):
        parser.add_argument(
            '--article',
            type=str,
            help='Article UUID to process'
        )
        parser.add_argument(
            '--batch',
            type=int,
            default=0,
            help='Process N articles in batch mode (0 = single article only)'
        )
        parser.add_argument(
            '--status',
            type=str,
            default='analyzing',
            choices=['new', 'analyzing', 'important', 'analyzed', 'all'],
            help='Filter articles by status (for batch mode)'
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be processed without actually processing'
        )
        parser.add_argument(
            '--sync',
            action='store_true',
            help='Run synchronously (do not use Celery)'
        )

    def handle(self, *args, **options):
        article_id = options['article']
        batch_size = options['batch']
        status_filter = options['status']
        dry_run = options['dry_run']
        sync = options['sync']

        # Single article mode
        if article_id:
            self._process_single_article(article_id, dry_run, sync)
            return

        # Batch mode
        if batch_size > 0:
            self._process_batch(batch_size, status_filter, dry_run, sync)
            return

        self.stdout.write(self.style.ERROR('Please specify --article or --batch'))

    def _process_single_article(self, article_id: str, dry_run: bool, sync: bool):
        """Process a single article."""
        try:
            article = Article.objects.get(id=article_id)
        except Article.DoesNotExist:
            raise CommandError(f'Article with ID {article_id} not found')

        self.stdout.write(f"Article: {article.title_origin[:60]}...")
        self.stdout.write(f"Status: {article.status}")
        self.stdout.write(f"Provider: {article.news_provider}")
        self.stdout.write(f"Language: {article.language}")

        if dry_run:
            self.stdout.write(self.style.WARNING('DRY RUN - not actually processing'))
            return

        if sync:
            self.stdout.write('Running synchronously...')
            result = extract_layer_a_e(article_id)
            self._print_result(result)
        else:
            self.stdout.write('Queueing Celery task...')
            task = extract_layer_a_e.delay(article_id)
            self.stdout.write(self.style.SUCCESS(f'Task queued: {task.id}'))

    def _process_batch(self, batch_size: int, status_filter: str, dry_run: bool, sync: bool):
        """Process a batch of articles."""
        # Build query
        query = Article.objects.all()

        if status_filter != 'all':
            query = query.filter(status=status_filter)

        # Exclude already analyzed unless explicitly requested
        if status_filter not in ['analyzed', 'all']:
            query = query.exclude(status='analyzed')

        # Require text content
        query = query.filter(text_origin__isnull=False).exclude(text_origin='')

        articles = query[:batch_size]

        self.stdout.write(f"Found {articles.count()} articles to process")

        for article in articles:
            self.stdout.write(f"  - {article.id}: {article.title_origin[:50]}... ({article.status})")

        if dry_run:
            self.stdout.write(self.style.WARNING('DRY RUN - not actually processing'))
            return

        if sync:
            self.stdout.write('Processing synchronously...')
            for article in articles:
                self.stdout.write(f"Processing {article.id}...")
                result = extract_layer_a_e(str(article.id))
                self._print_result(result)
        else:
            self.stdout.write('Queueing Celery tasks...')
            for article in articles:
                task = extract_layer_a_e.delay(str(article.id))
                self.stdout.write(f"  Queued: {article.id} -> {task.id}")

            self.stdout.write(self.style.SUCCESS(f'{articles.count()} tasks queued'))

    def _print_result(self, result: dict):
        """Print extraction result."""
        if result.get('success'):
            self.stdout.write(self.style.SUCCESS('SUCCESS'))
            self.stdout.write(f"  Status: {result.get('status')}")
            stats = result.get('stats', {})
            self.stdout.write(f"  Events: {stats.get('events', 0)}")
            self.stdout.write(f"  Locations: {stats.get('locations', 0)}")
            self.stdout.write(f"  Actors: {stats.get('actors', 0)}")
            self.stdout.write(f"  Relationships: {stats.get('relationships', 0)}")
            self.stdout.write(f"  Claims: {stats.get('claims', 0)}")

            if result.get('parse_errors'):
                self.stdout.write(self.style.WARNING(f"  Parse errors: {result['parse_errors']}"))
        else:
            self.stdout.write(self.style.ERROR(f"FAILED: {result.get('error', 'Unknown error')}"))
