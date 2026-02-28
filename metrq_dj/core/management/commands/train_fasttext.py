# metrq_dj/core/management/commands/train_fasttext.py
"""
Management command to train FastText importance classifier.
Usage: python manage.py train_fasttext [--epochs 25] [--lr 1.0] [--word-ngrams 2]
"""
import os
import tempfile
import random
from pathlib import Path
from datetime import timedelta
from typing import List, Tuple

from django.core.management.base import BaseCommand, CommandError
from django.conf import settings
from django.utils import timezone

from core.models import Article

import structlog

logger = structlog.get_logger()


class Command(BaseCommand):
    help = 'Train FastText classifier on articles with known importance labels'

    def add_arguments(self, parser):
        parser.add_argument(
            '--epochs',
            type=int,
            default=25,
            help='Number of training epochs (default: 25)'
        )
        parser.add_argument(
            '--lr',
            type=float,
            default=1.0,
            help='Learning rate (default: 1.0)'
        )
        parser.add_argument(
            '--word-ngrams',
            type=int,
            default=2,
            help='Max length of word n-gram (default: 2)'
        )
        parser.add_argument(
            '--dim',
            type=int,
            default=100,
            help='Size of word vectors (default: 100)'
        )
        parser.add_argument(
            '--output',
            type=str,
            default=None,
            help=f'Output path (default: {settings.FASTTEXT_MODEL_PATH})'
        )
        parser.add_argument(
            '--days',
            type=int,
            default=30,
            help='Number of days of history to use for training (default: 30)'
        )
        parser.add_argument(
            '--test-split',
            type=float,
            default=0.1,
            help='Fraction of data to use for validation (default: 0.1)'
        )

    def handle(self, *args, **options):
        try:
            import fasttext
        except ImportError:
            raise CommandError(
                "fasttext module not found. Install with: pip install fasttext"
            )

        output_path = options['output'] or settings.FASTTEXT_MODEL_PATH
        days = options['days']

        self.stdout.write(f"Collecting articles from last {days} days...")

        # Fetch labeled articles
        cutoff_date = timezone.now() - timedelta(days=days)

        important_articles = Article.objects.filter(
            status='analyzing',
            scraped_at__gte=cutoff_date,
            importance_score__isnull=False
        ).values_list('title_origin', 'title_translated', 'text_origin', 'importance_score')[:5000]

        unimportant_articles = Article.objects.filter(
            status='skipped',
            scraped_at__gte=cutoff_date,
            importance_score__isnull=False
        ).values_list('title_origin', 'title_translated', 'text_origin', 'importance_score')[:5000]

        self.stdout.write(
            f"Found {len(important_articles)} important, "
            f"{len(unimportant_articles)} unimportant articles"
        )

        if len(important_articles) < 50 or len(unimportant_articles) < 50:
            raise CommandError(
                f"Insufficient training data. Need at least 50 of each class, "
                f"got {len(important_articles)} important and {len(unimportant_articles)} unimportant."
            )

        # Prepare training data
        training_lines = []

        for title, title_trans, text, score in important_articles:
            label = "__label__important"
            content = self._prepare_text(title, title_trans, text)
            training_lines.append(f"{label} {content}")

        for title, title_trans, text, score in unimportant_articles:
            label = "__label__not_important"
            content = self._prepare_text(title, title_trans, text)
            training_lines.append(f"{label} {content}")

        # Shuffle data
        random.shuffle(training_lines)

        # Split train/test
        split_idx = int(len(training_lines) * (1 - options['test_split']))
        train_lines = training_lines[:split_idx]
        test_lines = training_lines[split_idx:]

        self.stdout.write(
            f"Training set: {len(train_lines)}, Validation set: {len(test_lines)}"
        )

        # Write to temp files
        with tempfile.NamedTemporaryFile(
                mode='w',
                suffix='.txt',
                delete=False,
                encoding='utf-8'
        ) as f:
            train_path = f.name
            f.write('\n'.join(train_lines))

        # Train model
        self.stdout.write("Training FastText model...")
        self.stdout.write(
            f"  epochs={options['epochs']}, lr={options['lr']}, "
            f"wordNgrams={options['word_ngrams']}, dim={options['dim']}"
        )

        try:
            model = fasttext.train_supervised(
                input=train_path,
                epoch=options['epochs'],
                lr=options['lr'],
                wordNgrams=options['word_ngrams'],
                dim=options['dim'],
                loss='softmax'
            )

            # Validate if test set exists
            if test_lines:
                with tempfile.NamedTemporaryFile(
                        mode='w',
                        suffix='.txt',
                        delete=False,
                        encoding='utf-8'
                ) as f:
                    test_path = f.name
                    f.write('\n'.join(test_lines))

                self.stdout.write("Running validation...")
                result = model.test(test_path)
                precision = result[1]
                recall = result[2]
                f1 = 2 * (precision * recall) / (precision + recall) if (precision + recall) > 0 else 0

                self.stdout.write(self.style.SUCCESS(
                    f"Validation Results:\n"
                    f"  Samples: {result[0]}\n"
                    f"  Precision: {precision:.4f}\n"
                    f"  Recall: {recall:.4f}\n"
                    f"  F1-Score: {f1:.4f}"
                ))

                os.unlink(test_path)

            # Save model
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            model.save_model(output_path)

            self.stdout.write(self.style.SUCCESS(
                f"Model saved to: {output_path}"
            ))

            # Log training event
            from core.models import ProviderLog
            ProviderLog.objects.create(
                level='info',
                message='FastText importance model trained successfully',
                data={
                    'train_samples': len(train_lines),
                    'test_samples': len(test_lines),
                    'precision': precision if 'precision' in dir() else None,
                    'recall': recall if 'recall' in dir() else None,
                    'output_path': output_path
                },
                worker_id='train_fasttext_command'
            )

        finally:
            os.unlink(train_path)

    def _prepare_text(self, title: str, title_trans: str, text: str) -> str:
        """Prepare article text for FastText training."""
        # Use translated title if available, otherwise original
        headline = title or title_trans or ""

        # Take first 500 chars of text body
        body = (text or "")[:500]

        # Simple normalization: lowercase, remove extra whitespace
        content = f"{headline} {body}".lower()
        content = ' '.join(content.split())  # Normalize whitespace

        return content
