"""
Article processing pipeline: Scrape → Translate → Enrich.
CPU-bound tasks wrapped in asyncio.to_thread() for async compatibility.
"""
import os
import asyncio
import json
from typing import Dict, Optional, Any
from datetime import datetime
from dataclasses import dataclass

import newspaper
# import deepl TODO: Change for free translator
import ollama
from bs4 import BeautifulSoup

import structlog

logger = structlog.get_logger()

OLLAMA_HOST = os.environ.get('OLLAMA_HOST', 'http://ollama:11434')
OLLAMA_MODEL = os.environ.get('OLLAMA_MODEL', 'qwen2.5-coder:1.5b')
DEEPL_API_KEY = os.environ.get('DEEPL_API_KEY', '')


@dataclass
class ArticleData:
    """Structured article data container."""
    url: str
    title_origin: str
    title_translated: Optional[str]
    text_origin: Optional[str]
    text_translated: Optional[str]
    language: str
    news_provider: str
    published_at: Optional[str]
    sentiment: Optional[float]
    bias: Optional[float]
    entities: Dict[str, Any]
    geotags: Dict[str, Any]
    scraped_at: str
    worker_id: str


class ArticleProcessor:
    """Stateless article processor with modular pipeline steps."""

    def __init__(self, worker_id: str = "worker1"):
        self.worker_id = worker_id
        self.deepl_translator = None
        self.ollama_client = ollama.Client(host=OLLAMA_HOST)

        if DEEPL_API_KEY:
            self.deepl_translator = deepl.Translator(DEEPL_API_KEY)

    async def process(self, url: str, metadata: Dict) -> Optional[ArticleData]:
        """
        Main entry point: Run full pipeline.
        Returns ArticleData or None if processing fails.
        """
        start_time = datetime.utcnow()

        try:
            # Step 1: Scrape (I/O bound, but newspaper is sync)
            article = await self._scrape_async(url)
            if not article:
                return None

            # Step 2: Detect/Set language
            lang = metadata.get('language', self._detect_language(article))

            # Step 3: Translate if needed (API call, async wrapper)
            title_translated = None
            text_translated = None
            if self.deepl_translator and lang in ['zh_cn', 'zh_tw', 'ru']:
                title_translated = await self._translate_async(article.title, target_lang='EN-US')
                text_translated = await self._translate_async(article.text[:5000], target_lang='EN-US')  # Limit text

            # Step 4: Enrich with LLM (CPU-bound)
            entities, geotags = await self._enrich_async(
                article.title + " " + article.text[:3000],  # Limit context
                lang
            )

            duration = (datetime.utcnow() - start_time).total_seconds() * 1000

            logger.info("article_processed",
                        url=url,
                        duration_ms=int(duration),
                        worker=self.worker_id,
                        language=lang)

            return ArticleData(
                url=url,
                title_origin=article.title,
                title_translated=title_translated,
                text_origin=article.text,
                text_translated=text_translated,
                language=lang,
                news_provider=metadata.get('source', 'unknown'),
                published_at=metadata.get('published'),
                sentiment=0.0,  # Placeholder for VADER implementation
                bias=0.5,  # Placeholder for bias calculation
                entities=entities,
                geotags=geotags,
                scraped_at=start_time.isoformat(),
                worker_id=self.worker_id
            )

        except Exception as e:
            logger.error("processing_failed", url=url, error=str(e), worker=self.worker_id)
            return None

    async def _scrape_async(self, url: str) -> Optional[newspaper.Article]:
        """Scrape article using newspaper3k in thread pool."""
        loop = asyncio.get_event_loop()

        def _scrape():
            try:
                article = newspaper.Article(url)
                article.download()
                article.parse()

                if not article.text or len(article.text) < 100:
                    logger.warning("article_too_short", url=url)
                    return None

                return article
            except Exception as e:
                logger.error("scraping_error", url=url, error=str(e))
                return None

        # Run CPU/sync I/O in thread
        return await loop.run_in_executor(None, _scrape)

    async def _translate_async(self, text: str, target_lang: str = 'EN-US') -> Optional[str]:
        """Translate text using DeepL in thread pool."""
        if not text or not self.deepl_translator:
            return None

        loop = asyncio.get_event_loop()

        def _translate():
            try:
                result = self.deepl_translator.translate_text(text, target_lang=target_lang)
                return result.text
            except Exception as e:
                logger.error("translation_failed", error=str(e))
                return None

        return await loop.run_in_executor(None, _translate)

    async def _enrich_async(self, text: str, language: str) -> tuple[Dict, Dict]:
        """
        Extract entities and geotags using Ollama.
        Uses asyncio.to_thread to prevent blocking on LLM inference.
        """
        prompt = f"""Analyze this news text and extract:
        1. Key entities (persons, organizations, terms, events)
        2. Geographic locations mentioned
        
        Respond ONLY in JSON format like:
        {{
          "persons": ["Name", "Name"],
          "organizations": ["Org"],
          "terms": ["term"],
          "events": ["Event"],
          "locations": ["City", "Country"]
        }}
        
        Text: {text[:2000]}
        
        JSON:"""

        try:
            loop = asyncio.get_event_loop()

            def _call_ollama():
                response = self.ollama_client.generate(
                    model=OLLAMA_MODEL,
                    prompt=prompt,
                    stream=False,
                    options={
                        'temperature': 0.3,  # Deterministic
                        'num_ctx': 4096
                    }
                )
                return response['response']

            # Run in thread (CPU intensive)
            result_text = await loop.run_in_executor(None, _call_ollama)

            # Parse JSON response
            # Sometimes model adds markdown code blocks, clean them
            json_str = result_text.strip()
            if '```json' in json_str:
                json_str = json_str.split('```json')[1].split('```')[0]
            elif '```' in json_str:
                json_str = json_str.split('```')[1].split('```')[0]

            data = json.loads(json_str.strip())

            entities = {
                'persons': data.get('persons', []),
                'organizations': data.get('organizations', []),
                'terms': data.get('terms', [])
            }
            geotags = {
                'locations': data.get('locations', [])
            }

            return entities, geotags

        except json.JSONDecodeError as e:
            logger.error("ollama_json_parse_failed",
                         response=result_text[:200],
                         error=str(e))
            return {}, {}
        except Exception as e:
            logger.error("ollama_enrichment_failed", error=str(e))
            return {}, {}

    def _detect_language(self, article: newspaper.Article) -> str:
        """Detect language from article."""
        # newspaper has built-in but we need specific codes
        lang = article.meta_lang or 'en'

        # Map to our codes
        mapping = {
            'zh': 'zh_cn',  # Simplified default, could be improved
            'cht': 'zh_tw',
            'ru': 'ru',
            'en': 'en'
        }
        return mapping.get(lang, 'en')

    def to_dict(self, data: ArticleData) -> Dict[str, Any]:
        """Convert ArticleData to dict for JSON serialization."""
        return {
            'url': data.url,
            'title_origin': data.title_origin,
            'title_translated': data.title_translated,
            'text_origin': data.text_origin[:10000] if data.text_origin else None,  # Limit storage
            'text_translated': data.text_translated[:10000] if data.text_translated else None,
            'language': data.language,
            'news_provider': data.news_provider,
            'published_at': data.published_at,
            'sentiment': data.sentiment,
            'bias': data.bias,
            'entities': data.entities,
            'geotags': data.geotags,
            'scraped_at': data.scraped_at,
            'worker_id': data.worker_id
        }
    