"""Harvest a picture dictionary: one curated image and one recording per headword.

A picture dictionary is what an image search is a poor substitute for. Someone chose the
photograph to mean the word, so `arm` is an arm rather than a coat of arms, and the recording is
a speaker rather than a synthesiser. Coverage is the trade: a few hundred concrete nouns against
a search that answers for anything, which is why this runs ahead of the search rather than
instead of it.

The catalogue is fetched once and cached, because a site laid out for reading is slow to walk
and its structure is the one thing here that will break. Everything downstream reads the cache,
so a layout change costs a re-harvest and nothing else.

Which dictionary, if any, is per-language and comes from the language config; without one the
provider disables itself.
"""

import html
import json
import pathlib
import re
import time
import typing
import urllib.parse

from . import language_config
from . import media

CACHE_FILENAME = 'picture_dictionary.json'
CHAPTER_PAUSE_SECONDS = 0.4

# A site laid out for reading has no API; these are the two structures the pages do have.
WORD_BLOCK_PATTERN = re.compile(r'<!-- Palabra -->(.*?)(?=<!-- Palabra -->|</section)', re.S)
IMAGE_PATTERN = re.compile(r'<img[^>]*src="([^"]+)"')
AUDIO_PATTERN = re.compile(r"play_mp3\('play','[^']*','([^']+)'")
CAPTION_PATTERN = re.compile(r'<p[^>]*>(.*?)</p>', re.S)
TAG_PATTERN = re.compile(r'<[^>]+>')
ARTICLE_PATTERN = re.compile(r'^\w+\s+(?=\w)')


class Catalogue:
    """Every headword the dictionary illustrates, keyed by the bare word."""

    def __init__(self, *, entries: typing.List[dict], language: language_config.LanguageConfig):
        self._language = language
        self._by_word: typing.Dict[str, dict] = {}
        for entry in entries:
            self._by_word.setdefault(self.bare(word=entry['word'], language=language), entry)

    def __len__(self) -> int:
        return len(self._by_word)

    def entry_for(self, *, word: str) -> typing.Optional[dict]:
        return self._by_word.get(self.bare(word=word, language=self._language))

    @staticmethod
    def bare(*, word: str, language: language_config.LanguageConfig) -> str:
        """The headword without its article, which the two sides spell differently."""
        cleaned = word.strip().lower()
        first, _, rest = cleaned.partition(' ')
        return rest if rest and first in language.articles else cleaned

    @classmethod
    def load(cls, *, language: language_config.LanguageConfig, data_directory: pathlib.Path) -> 'Catalogue':
        path = data_directory / media.CACHE_DIRECTORY_NAME / CACHE_FILENAME
        entries = json.loads(path.read_text(encoding='utf-8')) if path.exists() else []
        return cls(entries=entries, language=language)

    @classmethod
    def harvest(
        cls, *, language: language_config.LanguageConfig, data_directory: pathlib.Path,
        throttles: media.HostThrottleRegistry, stats: media.Stats,
    ) -> 'Catalogue':
        settings = language.media.get('picture_dictionary', {})
        entries: typing.List[dict] = []
        for url in cls._chapters(settings=settings, throttles=throttles, stats=stats):
            entries.extend(cls._entries_on(url=url, throttles=throttles, stats=stats))
            time.sleep(CHAPTER_PAUSE_SECONDS)
        path = data_directory / media.CACHE_DIRECTORY_NAME / CACHE_FILENAME
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(entries, ensure_ascii=False, indent=1), encoding='utf-8')
        return cls(entries=entries, language=language)

    @staticmethod
    def _chapters(*, settings: dict, throttles: media.HostThrottleRegistry, stats: media.Stats) -> typing.List[str]:
        """The site is a WordPress; its own page index is more reliable than its navigation."""
        template = settings.get('index_url', '')
        marker = settings.get('chapter_marker', '')
        if not template or not marker:
            return []
        links: typing.List[str] = []
        for number in range(1, settings.get('index_pages', 3) + 1):
            response = media.HttpClient.get_text(
                url=template.format(page=number),
                throttle=throttles.for_url(url=template.format(page=number)),
                stats=stats,
            )
            if response.status != media.FetchStatus.FOUND or response.text is None:
                continue
            links.extend(page['link'] for page in json.loads(response.text) if marker in page.get('slug', ''))
        return sorted(set(links))

    @classmethod
    def _entries_on(cls, *, url: str, throttles: media.HostThrottleRegistry, stats: media.Stats) -> typing.List[dict]:
        response = media.HttpClient.get_text(url=url, throttle=throttles.for_url(url=url), stats=stats)
        if response.status != media.FetchStatus.FOUND or response.text is None:
            return []
        found = []
        for block in WORD_BLOCK_PATTERN.findall(response.text):
            image = IMAGE_PATTERN.search(block)
            caption = CAPTION_PATTERN.search(block)
            if image is None or caption is None:
                continue
            pieces = [part.strip() for part in TAG_PATTERN.split(html.unescape(caption.group(1))) if part.strip()]
            if not pieces:
                continue
            audio = AUDIO_PATTERN.search(block)
            found.append({
                'word': pieces[0],
                'image': urllib.parse.urljoin(url, image.group(1)),
                'audio': audio.group(1) if audio is not None else '',
            })
        return found


class PictureDictionaryProvider:
    """Serves both an image and a recording for a headword the dictionary happens to cover."""

    NAME = 'picture_dictionary'

    @staticmethod
    def is_configured(*, language: language_config.LanguageConfig) -> bool:
        return bool(language.media.get('picture_dictionary', {}).get('index_url'))

    @staticmethod
    def image(
        *, lemma: str, catalogue: Catalogue, throttles: media.HostThrottleRegistry, stats: media.Stats,
    ) -> typing.Optional[media.ImageResult]:
        entry = catalogue.entry_for(word=lemma)
        if entry is None or not entry.get('image'):
            return None
        downloaded = media.MediaDownloader.fetch_and_validate_image(
            url=entry['image'], throttles=throttles, stats=stats,
        )
        if downloaded is None:
            return None
        content, extension = downloaded
        return media.ImageResult(
            content=content,
            extension=extension,
            source=PictureDictionaryProvider.NAME,
            license='',
            attribution=urllib.parse.urlsplit(entry['image']).netloc,
        )

    @staticmethod
    def audio(
        *, lemma: str, catalogue: Catalogue, throttles: media.HostThrottleRegistry, stats: media.Stats,
    ) -> typing.Optional[typing.Tuple[bytes, str]]:
        entry = catalogue.entry_for(word=lemma)
        if entry is None or not entry.get('audio'):
            return None
        extension = entry['audio'].rsplit('.', 1)[-1].lower()
        content = media.MediaDownloader.fetch_and_validate_audio(
            url=entry['audio'], extension=extension, throttles=throttles, stats=stats,
        )
        return None if content is None else (content, extension)
