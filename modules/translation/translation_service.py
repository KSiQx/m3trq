# modules/translation/translation_service.py
"""
MetrQ Translation Service
Multi-service translation with intelligent fallback and text chunking.
"""
import re
import time
import random
import logging
from typing import List, Tuple, Optional, Dict
from dataclasses import dataclass

import translators as ts

logger = logging.getLogger(__name__)


@dataclass
class TranslationResult:
    """Result container for translation operations."""
    text: Optional[str]
    service: Optional[str]
    success: bool
    errors: List[str]


class TranslationService:
    """
    Translation service with fallback chain and text chunking.

    Supports multiple online translation services with automatic fallback
    to local LLM when all online services fail.
    """

    # Service priority by source language (empirical quality ranking)
    SERVICE_PRIORITY: Dict[str, List[str]] = {
        'zh_cn': ['baidu', 'google', 'yandex'],  # Baidu best for Simplified Chinese
        'zh_tw': ['google', 'yandex', 'baidu'],  # Google better for Traditional
        'en': ['google', 'yandex', 'bing'],  # English sources
        'ru': ['google', 'yandex'],  # if translation to Russian will be implemented
    }

    # Language code mapping: project codes -> translators library codes
    LANG_CODE_MAP: Dict[str, str] = {
        'zh_cn': 'zh',
        'zh_tw': 'zh-CHT',
        'en': 'en',
        'ru': 'ru',
    }

    # Target language is always Russian until translation to Russian will be implemented
    TARGET_LANG = 'ru'

    # Chunk size limits
    ONLINE_CHUNK_SIZE = 4500  # Conservative limit for online APIs
    LLM_CHUNK_SIZE = 1500  # Optimal for HY-MT model

    def __init__(self, use_human_delay: bool = True):
        self.use_human_delay = use_human_delay
        self.errors: List[str] = []

    def _human_delay(self, min_sec: float = 2.0, max_sec: float = 5.0):
        """Add random human-like delay between requests."""
        if self.use_human_delay:
            delay = random.uniform(min_sec, max_sec)
            time.sleep(delay)

    def _split_text_en(self, text: str, chunk_size: int) -> List[str]:
        """
        Split English text by sentences.
        Preserves sentence boundaries for natural translation.
        """
        # Split by sentence-ending punctuation followed by whitespace
        sentences = re.split(r'(?<=[.!?])\s+', text)
        return self._combine_into_chunks(sentences, chunk_size)

    def _split_text_zh(self, text: str, chunk_size: int) -> List[str]:
        """
        Split Chinese text by Chinese punctuation.
        Preserves natural reading boundaries.
        """
        # Split by Chinese sentence endings
        sentences = re.split(r'(?<=[。！？])\s*', text)
        # Remove empty strings
        sentences = [s.strip() for s in sentences if s.strip()]
        return self._combine_into_chunks(sentences, chunk_size)

    def _combine_into_chunks(self, segments: List[str], chunk_size: int) -> List[str]:
        """Combine segments into chunks respecting size limit."""
        chunks = []
        current_chunk = []
        current_size = 0

        for segment in segments:
            segment_size = len(segment)

            # If single segment exceeds chunk size, split it forcibly
            if segment_size > chunk_size:
                if current_chunk:
                    chunks.append(' '.join(current_chunk))
                    current_chunk = []
                    current_size = 0

                # Split long segment into parts
                for i in range(0, len(segment), chunk_size):
                    part = segment[i:i + chunk_size]
                    chunks.append(part)
                continue

            # Check if adding this segment would exceed chunk size
            if current_size + segment_size + 1 > chunk_size and current_chunk:
                chunks.append(' '.join(current_chunk))
                current_chunk = [segment]
                current_size = segment_size
            else:
                current_chunk.append(segment)
                current_size += segment_size + 1

        # Don't forget the last chunk
        if current_chunk:
            chunks.append(' '.join(current_chunk))

        return chunks

    def chunk_text(self, text: str, source_lang: str, chunk_size: int = None) -> List[str]:
        """
        Split text into translation-friendly chunks.

        Args:
            text: Text to split
            source_lang: Source language code
            chunk_size: Maximum chunk size (uses default if None)

        Returns:
            List of text chunks
        """
        if not text:
            return []

        size = chunk_size or self.ONLINE_CHUNK_SIZE

        if source_lang in ('zh_cn', 'zh_tw'):
            return self._split_text_zh(text, size)
        else:
            return self._split_text_en(text, size)

    def translate_chunk(
            self,
            text: str,
            from_lang: str,
            service: str,
            max_retries: int = 2
    ) -> Optional[str]:
        """
        Translate a single chunk using specified service.

        Args:
            text: Text to translate
            from_lang: Source language (translators library code)
            service: Service name ('google', 'baidu', 'yandex', 'bing')
            max_retries: Number of retry attempts

        Returns:
            Translated text or None if failed
        """
        for attempt in range(max_retries):
            try:
                self._human_delay(3.0, 6.0)  # Delay for chunks

                result = ts.translate_text(
                    query_text=text,
                    translator=service,
                    from_language=from_lang,
                    to_language=self.TARGET_LANG,
                    timeout=30
                )

                if result and len(result.strip()) > 0:
                    logger.debug(f"Translation successful with {service}")
                    return result.strip()

                logger.warning(f"Empty result from {service}, attempt {attempt + 1}")

            except Exception as e:
                logger.warning(f"Translation failed with {service}: {e}, attempt {attempt + 1}")
                if attempt < max_retries - 1:
                    self._human_delay(5.0, 7.0)

        return None

    def translate_with_service(
            self,
            chunks: List[str],
            from_lang: str,
            service: str
    ) -> Tuple[Optional[List[str]], bool]:
        """
        Translate all chunks with a single service.

        Returns:
            Tuple of (translated_chunks, all_successful)
        """
        translated = []

        for i, chunk in enumerate(chunks):
            result = self.translate_chunk(chunk, from_lang, service)

            if result is None:
                logger.error(f"Service {service} failed on chunk {i + 1}/{len(chunks)}")
                return None, False

            translated.append(result)
            logger.debug(f"Chunk {i + 1}/{len(chunks)} translated with {service}")
            self._human_delay(min_sec=1.0, max_sec=2.0)

        return translated, True

    def translate(self, text: str, source_lang: str) -> TranslationResult:
        """
        Main translation method with fallback chain.

        Args:
            text: Text to translate
            source_lang: Source language code (project codes: 'zh_cn', 'en', etc.)

        Returns:
            TranslationResult with translated text and metadata
        """
        self.errors = []

        if not text or not text.strip():
            return TranslationResult(text="", service=None, success=True, errors=[])

        # Map project language code to translators library code
        from_lang = self.LANG_CODE_MAP.get(source_lang, 'auto')

        # Get service priority for this language
        services = self.SERVICE_PRIORITY.get(source_lang, ['google', 'yandex'])

        # Chunk the text
        chunks = self.chunk_text(text, source_lang)
        logger.info(f"Text split into {len(chunks)} chunks for translation")

        # Try each service in priority order
        for service in services:
            logger.info(f"Attempting translation with {service}")

            translated_chunks, success = self.translate_with_service(
                chunks, from_lang, service
            )

            if success and translated_chunks:
                full_translation = ' '.join(translated_chunks)
                return TranslationResult(
                    text=full_translation,
                    service=service,
                    success=True,
                    errors=[]
                )

            self.errors.append(f"{service}: failed")
            self._human_delay(1.0, 3.0)  # delay between services

        # All online services failed
        logger.error("All online translation services failed")
        return TranslationResult(
            text=None,
            service=None,
            success=False,
            errors=self.errors
        )


def translate_to_russian(text: str, source_lang: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Convenience function for Russian translation.

    Args:
        text: Text to translate
        source_lang: Source language code ('zh_cn', 'zh_tw', 'en', 'ru')

    Returns:
        Tuple of (translated_text, service_used)
    """
    service = TranslationService()
    result = service.translate(text, source_lang)

    if result.success:
        return result.text, result.service

    return None, None
