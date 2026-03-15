"""
Local LLM-based translation using HY-MT model via Ollama.
Fallback when all online services fail.
"""
import re
import os
import json
import logging
import requests
from typing import List, Optional

from .translation_service import TranslationService

logger = logging.getLogger(__name__)

OLLAMA_TRANSLATE_HOST = os.environ.get('OLLAMA_TRANSLATE_HOST', 'http://ollama-translate:11434')
OLLAMA_MT_MODEL = os.environ.get('OLLAMA_MT_MODEL', 'sun_leaf/HY-MT:1.8b')


def normalize_text(text: str) -> str:
    """Clean and normalize text before translation."""
    # Remove HTML tags
    text = re.sub(r'<[^>]+>', ' ', text)
    # Normalize whitespace
    text = re.sub(r'\s+', ' ', text)
    # Remove control characters
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]', '', text)
    return text.strip()


def get_prompt_for_language(text: str, source_lang: str) -> str:
    """
    Generate appropriate prompt based on source language.
    HY-MT is optimized for Chinese-English-Russian translation.
    """
    prompts = {
        'zh_cn': f"将以下文本翻译为俄语，注意只需要输出翻译后的结果，不要额外解释：\n{text}",
        'zh_tw': f"將以下文本翻譯為俄語，注意只需要輸出翻譯後的結果，不要額外解釋：\n{text}",
        'en': f"Translate the following text to Russian. Output only the translation, no explanations:\n{text}",
        'ru': text,  # Already Russian
    }

    return prompts.get(source_lang, prompts['en'])


def translate_chunk_with_llm(
        chunk: str,
        source_lang: str,
        timeout: int = 120
) -> Optional[str]:
    """
    Translate a single chunk using local HY-MT model.

    Args:
        chunk: Text chunk to translate
        source_lang: Source language code
        timeout: Request timeout in seconds

    Returns:
        Translated text or None if failed
    """
    try:
        prompt = get_prompt_for_language(chunk, source_lang)

        response = requests.post(
            f"{OLLAMA_TRANSLATE_HOST}/api/generate",
            json={
                "model": OLLAMA_MT_MODEL,
                "prompt": prompt,
                "stream": False,
                "options": {
                    "temperature": 0.1,  # Low temperature for translation accuracy
                    "num_ctx": 2048,
                    "num_predict": len(chunk) * 2,  # Allow longer output
                }
            },
            timeout=timeout,
            headers={"Content-Type": "application/json"}
        )

        response.raise_for_status()
        data = response.json()

        result = data.get('response', '').strip()

        # Clean up common LLM artifacts
        result = re.sub(r'^(Translation:|Перевод:|翻译：)\s*', '', result, flags=re.IGNORECASE)
        result = result.strip('"\'')

        if result and len(result) > 10:  # Sanity check
            return result

        logger.warning(f"LLM returned empty or too short result: {result[:100]}")
        return None

    except requests.Timeout:
        logger.error(f"LLM translation timeout after {timeout}s")
        return None
    except requests.RequestException as e:
        logger.error(f"LLM translation request failed: {e}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error in LLM translation: {e}")
        return None


def translate_with_hy_mt(text: str, source_lang: str) -> Optional[str]:
    """
    Translate text using local HY-MT model with chunking.

    Args:
        text: Text to translate
        source_lang: Source language code

    Returns:
        Translated text or None if failed
    """
    if not text or not text.strip():
        return ""

    # Normalize text
    text = normalize_text(text)

    # Use smaller chunks for LLM
    service = TranslationService(use_human_delay=False)
    chunks = service.chunk_text(text, source_lang, chunk_size=service.LLM_CHUNK_SIZE)

    logger.info(f"HY-MT: Translating {len(chunks)} chunks")

    translated_chunks = []

    for i, chunk in enumerate(chunks):
        logger.debug(f"HY-MT: Translating chunk {i + 1}/{len(chunks)}")

        result = translate_chunk_with_llm(chunk, source_lang)

        if result is None:
            logger.error(f"HY-MT failed on chunk {i + 1}")
            return None

        translated_chunks.append(result)

    # Join with space, preserving paragraph structure if possible
    full_translation = ' '.join(translated_chunks)

    logger.info("HY-MT translation completed successfully")
    return full_translation
