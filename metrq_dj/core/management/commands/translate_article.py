"""
Management command to manually trigger article translation.
"""
import logging
from django.core.management.base import BaseCommand, CommandError
from core.models import Article
from core.tasks.translation import translate_article

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Translate a specific article or process pending translations'

    def add_arguments(self, parser):
        parser.add_argument(
            '--id',
            type=str,
            help='Article UUID to translate'
        )
        parser.add_argument(
            '--status',
            type=str,
            default='analyzed',
            choices=['new', 'analyzing', 'analyzed', 'translating', 'translated', 'failed', 'all'],
            help='Process all articles with this status'
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be translated without doing it'
        )
        parser.add_argument(
            '--sync',
            action='store_true',
            help='Run translation synchronously (blocking)'
        )

    def handle(self, *args, **options):
        article_id = options.get('id')
        status = options.get('status')
        dry_run = options.get('dry_run')
        sync = options.get('sync')

        if article_id:
            # Translate specific article
            try:
                article = Article.objects.get(id=article_id)
            except Article.DoesNotExist:
                raise CommandError(f'Article {article_id} not found')

            self.stdout.write(f'Article: {article.title_origin[:60]}...')
            self.stdout.write(f'Status: {article.status}')
            self.stdout.write(f'Language: {article.language}')

            if dry_run:
                self.stdout.write(self.style.WARNING('DRY RUN - not translating'))
                return

            if sync:
                # Run synchronously for debugging
                result = translate_article.run(str(article.id))
                self.stdout.write(f'Result: {result}')
            else:
                # Queue async task
                task = translate_article.delay(str(article.id))
                self.stdout.write(self.style.SUCCESS(f'Translation queued: {task.id}'))

        else:
            # Batch process by status
            if status == 'all':
                articles = Article.objects.filter(
                    status__in=['analyzed', 'failed']
                ).exclude(text_translated__isnull=False)
            else:
                articles = Article.objects.filter(status=status)

            count = articles.count()
            self.stdout.write(f'Found {count} articles with status="{status}"')

            if dry_run:
                for article in articles[:10]:
                    self.stdout.write(f'  - {article.id}: {article.title_origin[:50]}...')
                if count > 10:
                    self.stdout.write(f'  ... and {count - 10} more')
                return

            # Queue all
            queued = 0
            for article in articles:
                translate_article.delay(str(article.id))
                queued += 1

            self.stdout.write(self.style.SUCCESS(f'Queued {queued} articles for translation'))
