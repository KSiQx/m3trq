"""
MetrQ Translation Module
Provides text translation with fallback chain and chunking support.
"""

from .translation_service import TranslationService, translate_to_russian
from .llm_translator import translate_with_hy_mt

__all__ = [
    'TranslationService',
    'translate_to_russian',
    'translate_with_hy_mt'
]
