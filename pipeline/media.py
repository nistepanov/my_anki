"""Fetch pronunciation audio and images from free public sources for words that have none.

One of several media providers alongside reword_media.py, which pulls media out of a source
app's own backup when one exists. Both write into the same data/<target>/media/ directory and
the same data/<target>/media.tsv manifest, so this script only ever fills an empty manifest
cell — it never overwrites one, and it never deletes a file. The 'source_backup' entry in a
language's provider lists is that other script's job; it appears there only so the ordering of
every source is documented in one place.

Reads cards.tsv when it exists, since only the enriched table carries the is_object and
translations_<pivot> columns images depend on; falls back to words.tsv (audio only) otherwise.

Every provider caches its raw API responses on disk, keyed by provider and lemma, so a re-run
that finds nothing new to do makes no network calls at all. A downloaded media file is its own
cache: it is never re-fetched once it exists on disk under its final name.
"""

import argparse
import concurrent.futures
import enum
import html
import json
import pathlib
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import typing
import urllib.error
import urllib.parse
import urllib.request

from . import reword_media
from . import language_config

USER_AGENT = 'my-anki-export/1.0 (personal Anki deck build)'
REQUEST_TIMEOUT_SECONDS = 15.0
MAX_ATTEMPTS = 5  # one initial attempt plus four retries
RETRY_BACKOFF_BASE_SECONDS = 1.0
RETRY_JITTER_MAX_SECONDS = 0.5
# Caps how long a single retry actually sleeps; a Retry-After header measured in hours would
# otherwise stall the whole run instead of just failing this one request.
MAX_RETRY_DELAY_SECONDS = 30.0

HTTP_STATUS_NOT_FOUND = 404
HTTP_STATUS_TOO_MANY_REQUESTS = 429

MAX_WORKERS_PER_HOST = 2
HOST_MIN_REQUEST_INTERVAL_SECONDS = 0.25
# Hosts that answer a normal pace with 429 and a request to stop. Served one at a time and far
# apart, they keep answering; the alternative is not a faster run but an empty one.
RELUCTANT_HOSTS = {'upload.wikimedia.org': 5.0}
# Bounds total in-flight work; the real concurrency ceiling is the per-host throttle above.
EXECUTOR_MAX_WORKERS = 8
# How often a long fetch flushes what it has, so a stopped run keeps its work.
MANIFEST_FLUSH_EVERY = 25

# The one audio format every Anki client plays; iOS refuses Ogg outright.
PORTABLE_AUDIO_EXTENSION = 'mp3'
CONVERTER_COMMAND = 'ffmpeg'

CARDS_FILENAME = 'cards.tsv'
WORDS_FILENAME = 'words.tsv'
MEDIA_DIRECTORY_NAME = 'media'
CACHE_DIRECTORY_NAME = 'cache'
MANIFEST_FILENAME = 'media.tsv'

# Openverse's own weak size floor lets through unusable thumbnails; 5 KB is comfortably above a
# placeholder icon but below any photo worth putting on a card.
MIN_IMAGE_CONTENT_BYTES = 5 * 1024
# Aspirational: without an imaging library, downloaded images are stored exactly as fetched and
# never resized down to this. See MediaValidator.image_extension_for.
IMAGE_MAX_LONGEST_SIDE_PIXELS = 500

WIKTIONARY_PARSE_URL_TEMPLATE = (
    'https://{host}/w/api.php?action=parse&page={lemma}&prop=wikitext&format=json&formatversion=2'
)
COMMONS_HOST = 'commons.wikimedia.org'
COMMONS_IMAGEINFO_URL_TEMPLATE = (
    f'https://{COMMONS_HOST}/w/api.php?action=query&format=json&prop=imageinfo'
    '&iiprop=url|size|mime&titles=File:{filename}'
)
SYNTHESIS_HOST = 'translate.google.com'
SYNTHESIS_URL_TEMPLATE = (
    f'https://{SYNTHESIS_HOST}/translate_tts?ie=UTF-8&client=tw-ob&tl={{code}}&q={{text}}'
)
SYNTHESIS_EXTENSION = 'mp3'
# A synthesiser answers for every word, so it must be asked politely and one at a time.
SYNTHESIS_MIN_REQUEST_INTERVAL_SECONDS = 0.6

OPENVERSE_HOST = 'api.openverse.org'
OPENVERSE_PAGE_SIZE = 5
OPENVERSE_SEARCH_URL_TEMPLATE = (
    f'https://{OPENVERSE_HOST}/v1/images/?q={{query}}&license_type=commercial&page_size={{page_size}}'
)
WIKIPEDIA_PAGEIMAGE_URL_TEMPLATE = (
    'https://{host}/w/api.php?action=query&format=json&prop=pageimages&piprop=original|name'
    '&titles={title}&redirects=1'
)
# Asked of the article's own wiki, which also answers for files kept on Commons.
WIKIPEDIA_FILE_LICENCE_URL_TEMPLATE = (
    'https://{host}/w/api.php?action=query&format=json&prop=imageinfo&iiprop=extmetadata|url'
    '&iiextmetadatafilter=LicenseShortName|Artist|NonFree&titles=File:{filename}'
)
WIKIPEDIA_FILE_CACHE_PREFIX = 'file:'
HTML_TAG_PATTERN = re.compile(r'<[^>]+>')

# Matches a pron-graf parameter name such as "1audio1", "audio2" or the plain "audio" a page
# uses when it carries only one recording — a numeric prefix marks which recording it is and a
# numeric suffix marks which speaker, but neither is present for a lone recording.
AUDIO_PARAM_NAME_PATTERN = re.compile(r'^\d*audio\d*$')
AUDIO_FILENAME_SUFFIXES = ('.ogg', '.wav', '.mp3')
PRON_GRAF_TEMPLATE_PATTERN = re.compile(r'\{\{pron-graf(?:\|([^{}]*))?\}\}')
# What an edition states a recording with when it has no pronunciation table: {{audio|en|En-us-word.ogg}}.
AUDIO_TEMPLATE_PATTERN = re.compile(r'\{\{audio\|([^{}]*)\}\}', re.IGNORECASE)


class AudioProviderName(enum.StrEnum):
    SOURCE_BACKUP = 'source_backup'
    PICTURE_DICTIONARY = 'picture_dictionary'
    WIKTIONARY = 'wiktionary'
    DICTIONARY = 'dictionary'
    SYNTHESIS = 'synthesis'


class ImageProviderName(enum.StrEnum):
    SOURCE_BACKUP = 'source_backup'
    PICTURE_DICTIONARY = 'picture_dictionary'
    OPENVERSE = 'openverse'
    WIKIPEDIA = 'wikipedia'


class FetchStatus(enum.StrEnum):
    FOUND = 'found'
    NOT_FOUND = 'not_found'
    FAILED = 'failed'


class TextResponse(typing.NamedTuple):
    status: FetchStatus
    text: typing.Optional[str]


class BinaryResponse(typing.NamedTuple):
    status: FetchStatus
    content: typing.Optional[bytes]
    content_type: typing.Optional[str]


class ImageResult(typing.NamedTuple):
    content: bytes
    extension: str
    source: str
    license: str
    attribution: str


class Counter:
    """A plain int is not safe to += from several worker threads at once; this is."""

    def __init__(self) -> None:
        self._value = 0
        self._lock = threading.Lock()

    def increment(self) -> None:
        with self._lock:
            self._value += 1

    @property
    def value(self) -> int:
        return self._value


class Stats:
    """Coverage counters reported in the final summary."""

    def __init__(self) -> None:
        self.failed_requests = Counter()
        self.audio_fetched = 0
        self.images_fetched = 0

    def summary(self, *, total_rows: int) -> str:
        return (
            f"rows: {total_rows}\n"
            f"audio fetched: {self.audio_fetched}\n"
            f"images fetched: {self.images_fetched}\n"
            f"failed requests: {self.failed_requests.value}"
        )


class HostThrottle:
    """Bounds concurrent requests to one host and spaces their start times apart."""

    def __init__(self, *, max_workers: int, min_interval_seconds: float) -> None:
        self._semaphore = threading.Semaphore(max_workers)
        self._lock = threading.Lock()
        self._next_allowed_time = 0.0
        self._min_interval_seconds = min_interval_seconds

    def __enter__(self) -> 'HostThrottle':
        self._semaphore.acquire()
        with self._lock:
            now = time.monotonic()
            delay = self._next_allowed_time - now
            if delay > 0:
                time.sleep(delay)
                now = time.monotonic()
            self._next_allowed_time = now + self._min_interval_seconds
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._semaphore.release()


class HostThrottleRegistry:
    """One HostThrottle per hostname, created lazily since download hosts aren't known upfront."""

    def __init__(self, *, max_workers_per_host: int, min_interval_seconds: float) -> None:
        self._max_workers_per_host = max_workers_per_host
        self._min_interval_seconds = min_interval_seconds
        self._lock = threading.Lock()
        self._throttles: typing.Dict[str, HostThrottle] = {}

    def for_host(self, *, host: str) -> HostThrottle:
        with self._lock:
            throttle = self._throttles.get(host)
            if throttle is None:
                interval = RELUCTANT_HOSTS.get(host)
                throttle = HostThrottle(
                    max_workers=1 if interval is not None else self._max_workers_per_host,
                    min_interval_seconds=interval if interval is not None else self._min_interval_seconds,
                )
                self._throttles[host] = throttle
            return throttle

    def for_url(self, *, url: str) -> HostThrottle:
        return self.for_host(host=urllib.parse.urlparse(url).hostname or '')


class HttpClient:
    """Shared GET-with-retries logic used by every provider, text or binary."""

    @staticmethod
    def get_text(*, url: str, throttle: HostThrottle, stats: Stats) -> TextResponse:
        status, content, _content_type = HttpClient._request(url=url, throttle=throttle, stats=stats)
        text = content.decode('utf-8') if status == FetchStatus.FOUND and content is not None else None
        return TextResponse(status=status, text=text)

    @staticmethod
    def get_binary(*, url: str, throttle: HostThrottle, stats: Stats) -> BinaryResponse:
        status, content, content_type = HttpClient._request(url=url, throttle=throttle, stats=stats)
        return BinaryResponse(status=status, content=content, content_type=content_type)

    @staticmethod
    def _request(
        *, url: str, throttle: HostThrottle, stats: Stats,
    ) -> typing.Tuple[FetchStatus, typing.Optional[bytes], typing.Optional[str]]:
        request = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
        for attempt in range(MAX_ATTEMPTS):
            retry_after_seconds = None
            with throttle:
                try:
                    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                        return FetchStatus.FOUND, response.read(), response.headers.get('Content-Type')
                except urllib.error.HTTPError as error:
                    if error.code == HTTP_STATUS_NOT_FOUND:
                        return FetchStatus.NOT_FOUND, None, None
                    if error.code == HTTP_STATUS_TOO_MANY_REQUESTS:
                        # A host that is already served one request at a time and seconds apart
                        # is not being overrun; it is refusing. Retrying costs minutes per word
                        # and changes nothing, and the failure is not cached, so the next run
                        # asks again anyway.
                        if urllib.parse.urlparse(url).hostname in RELUCTANT_HOSTS:
                            stats.failed_requests.increment()
                            return FetchStatus.FAILED, None, None
                        retry_after_seconds = HttpClient._parse_retry_after(header_value=error.headers.get('Retry-After'))
                except (urllib.error.URLError, OSError, TimeoutError):
                    pass
            if attempt < MAX_ATTEMPTS - 1:
                time.sleep(HttpClient._backoff_delay(attempt=attempt, retry_after_seconds=retry_after_seconds))
        stats.failed_requests.increment()
        return FetchStatus.FAILED, None, None

    @staticmethod
    def _backoff_delay(*, attempt: int, retry_after_seconds: typing.Optional[float]) -> float:
        base_delay = retry_after_seconds if retry_after_seconds is not None else RETRY_BACKOFF_BASE_SECONDS * (2 ** attempt)
        base_delay = min(base_delay, MAX_RETRY_DELAY_SECONDS)
        return base_delay + random.uniform(0, RETRY_JITTER_MAX_SECONDS)

    @staticmethod
    def _parse_retry_after(*, header_value: typing.Optional[str]) -> typing.Optional[float]:
        if not header_value:
            return None
        try:
            return float(header_value)
        except ValueError:
            return None


class PortableMedia:
    """Recodes a downloaded file into something every Anki client can actually open.

    Anki syncs media untouched, so whatever a source happened to serve is what a phone tries to
    play — and an Ogg recording, perfectly normal on a desktop, is silently unplayable on iOS.
    Fixing it here is the only place it gets fixed once for every device instead of per client.

    Pictures get the same treatment for a different reason: a search result is sized for a web
    page, is an order of magnitude larger than a card needs, and every byte of it is synced to
    the phone. Without a converter installed both conversions are skipped rather than failing —
    an oversized picture is a nuisance, a missing one is a hole in the card.
    """

    AUDIO_ARGUMENTS = ('-codec:a', 'libmp3lame', '-q:a', '6', '-ar', '22050', '-ac', '1')
    IMAGE_FILTER = (
        f"scale='min({IMAGE_MAX_LONGEST_SIDE_PIXELS},iw)':'min({IMAGE_MAX_LONGEST_SIDE_PIXELS},ih)'"
        ':force_original_aspect_ratio=decrease'
    )

    @staticmethod
    def is_available() -> bool:
        return shutil.which(CONVERTER_COMMAND) is not None

    @classmethod
    def audio(cls, *, path: pathlib.Path) -> pathlib.Path:
        if path.suffix.lstrip('.').lower() == PORTABLE_AUDIO_EXTENSION or not cls.is_available():
            return path
        converted = path.with_suffix(f'.{PORTABLE_AUDIO_EXTENSION}')
        if cls._convert(source=path, target=converted, arguments=cls.AUDIO_ARGUMENTS):
            path.unlink(missing_ok=True)
            return converted
        return path

    @classmethod
    def image(cls, *, path: pathlib.Path) -> pathlib.Path:
        if not cls.is_available():
            return path
        resized = path.with_name(f'{path.stem}.resized{path.suffix}')
        if cls._convert(source=path, target=resized, arguments=('-vf', cls.IMAGE_FILTER, '-q:v', '4')):
            resized.replace(path)
        else:
            resized.unlink(missing_ok=True)
        return path

    @staticmethod
    def _convert(*, source: pathlib.Path, target: pathlib.Path, arguments: typing.Sequence[str]) -> bool:
        completed = subprocess.run(
            [CONVERTER_COMMAND, '-y', '-loglevel', 'error', '-i', str(source), *arguments, str(target)],
            capture_output=True,
        )
        return completed.returncode == 0 and target.exists() and target.stat().st_size > 0


class MediaValidator:
    """Sniffs a downloaded blob's real type from its magic bytes rather than trusting a URL."""

    OGG_MAGIC = b'OggS'
    RIFF_MAGIC = b'RIFF'
    WEBP_RIFF_TYPE = b'WEBP'
    ID3_MAGIC = b'ID3'
    MP3_FRAME_SYNC_BYTE = 0xFF
    MP3_FRAME_SYNC_MASK = 0xE0
    JPEG_MAGIC = b'\xff\xd8\xff'
    PNG_MAGIC = b'\x89PNG\r\n\x1a\n'

    @staticmethod
    def audio_matches(*, extension: str, content: bytes) -> bool:
        if extension == 'ogg':
            return content.startswith(MediaValidator.OGG_MAGIC)
        if extension == 'wav':
            return content.startswith(MediaValidator.RIFF_MAGIC)
        if extension == 'mp3':
            return content.startswith(MediaValidator.ID3_MAGIC) or MediaValidator._has_mp3_frame_sync(content=content)
        return False

    @staticmethod
    def _has_mp3_frame_sync(*, content: bytes) -> bool:
        return (
            len(content) >= 2
            and content[0] == MediaValidator.MP3_FRAME_SYNC_BYTE
            and (content[1] & MediaValidator.MP3_FRAME_SYNC_MASK) == MediaValidator.MP3_FRAME_SYNC_MASK
        )

    @staticmethod
    def image_extension_for(*, content: bytes) -> typing.Optional[str]:
        """Real type by magic bytes; the 500px cap in the module docstring is not applied here."""
        if content.startswith(MediaValidator.JPEG_MAGIC):
            return 'jpg'
        if content.startswith(MediaValidator.PNG_MAGIC):
            return 'png'
        if content.startswith(MediaValidator.RIFF_MAGIC) and content[8:12] == MediaValidator.WEBP_RIFF_TYPE:
            return 'webp'
        return None


class Cache:
    """On-disk JSON cache, one file per (provider, identifier). Also stores negative results.

    A network failure is never stored here — only an affirmative answer from the API, found or
    not — so a failed request is retried on the next run instead of being remembered forever.
    """

    def __init__(self, *, root: pathlib.Path) -> None:
        self._root = root

    def load(self, *, provider: str, identifier: str) -> typing.Optional[dict]:
        path = self._path_for(provider=provider, identifier=identifier)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            return None

    def store(self, *, provider: str, identifier: str, payload: dict) -> None:
        path = self._path_for(provider=provider, identifier=identifier)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')

    def _path_for(self, *, provider: str, identifier: str) -> pathlib.Path:
        safe_identifier = identifier.replace('/', '_')
        return self._root / provider / f'{safe_identifier}.json'


class DictionaryAudioProvider:
    """A dictionary site that speaks a headword back over a plain URL.

    Checked and found to answer for any input at all, including nonsense, so it is a synthesiser
    with a dictionary's front door rather than a library of recordings — it belongs ahead of a
    general-purpose synthesiser because it is tuned for one language, and behind anything that
    serves real speakers. It always answers, so no provider after it is ever reached.

    Which site — if any — is a per-language question, so the URL template comes from the language
    config and the provider disables itself when none is configured.
    """

    NAME = AudioProviderName.DICTIONARY

    @staticmethod
    def fetch(
        *, lemma: str, language: language_config.LanguageConfig,
        throttles: HostThrottleRegistry, stats: Stats,
    ) -> typing.Optional[typing.Tuple[bytes, str]]:
        template = language.media.get('audio_dictionary_url', '')
        if not template:
            return None
        url = template.format(text=urllib.parse.quote(lemma), code=language.target)
        content = MediaDownloader.fetch_and_validate_audio(
            url=url, extension=SYNTHESIS_EXTENSION, throttles=throttles, stats=stats,
        )
        return None if content is None else (content, SYNTHESIS_EXTENSION)


class SynthesisAudioProvider:
    """Last resort: speech synthesised from the headword, in the language being learned.

    Works for every word and every language, which is exactly why it comes last — a recording of
    a real speaker teaches pronunciation better than any synthesiser, so it is only worth reaching
    for once the recorded sources have had their turn.
    """

    NAME = AudioProviderName.SYNTHESIS

    @staticmethod
    def fetch(
        *, lemma: str, language: language_config.LanguageConfig,
        throttles: HostThrottleRegistry, stats: Stats,
    ) -> typing.Optional[typing.Tuple[bytes, str]]:
        url = SYNTHESIS_URL_TEMPLATE.format(code=language.target, text=urllib.parse.quote(lemma))
        content = MediaDownloader.fetch_and_validate_audio(
            url=url, extension=SYNTHESIS_EXTENSION, throttles=throttles, stats=stats,
        )
        return None if content is None else (content, SYNTHESIS_EXTENSION)


class MediaDownloader:
    """Downloads a candidate file and validates it before it is ever written to disk."""

    @staticmethod
    def fetch_and_validate_audio(
        *, url: str, extension: str, throttles: HostThrottleRegistry, stats: Stats,
    ) -> typing.Optional[bytes]:
        response = HttpClient.get_binary(url=url, throttle=throttles.for_url(url=url), stats=stats)
        if response.status != FetchStatus.FOUND or response.content is None:
            return None
        if not MediaValidator.audio_matches(extension=extension, content=response.content):
            return None
        return response.content

    @staticmethod
    def fetch_and_validate_image(
        *, url: str, throttles: HostThrottleRegistry, stats: Stats,
    ) -> typing.Optional[typing.Tuple[bytes, str]]:
        response = HttpClient.get_binary(url=url, throttle=throttles.for_url(url=url), stats=stats)
        if response.status != FetchStatus.FOUND or response.content is None:
            return None
        if len(response.content) < MIN_IMAGE_CONTENT_BYTES:
            return None
        if response.content_type is not None and 'image' not in response.content_type:
            return None
        extension = MediaValidator.image_extension_for(content=response.content)
        if extension is None:
            return None
        return response.content, extension


class WiktionaryAudioProvider:
    """Resolves a lemma's own-language pronunciation recording from its Wiktionary page.

    A page's pron-graf template lists recordings for every language that shares the spelling,
    so candidates are filtered by the language's own filename markers before any is trusted.
    """

    NAME = AudioProviderName.WIKTIONARY

    @staticmethod
    def resolve(
        *, lemma: str, language: language_config.LanguageConfig, cache: Cache,
        throttles: HostThrottleRegistry, stats: Stats,
    ) -> typing.Optional[typing.Tuple[str, str]]:
        """Returns (filename, download_url) for the first candidate that resolves, or None."""
        cached = cache.load(provider=WiktionaryAudioProvider.NAME, identifier=lemma)
        if cached is not None and not cached.get('found'):
            return None

        if cached is not None:
            wikitext = cached.get('wikitext', '')
            resolutions = cached.get('resolutions', {})
        else:
            wikitext = WiktionaryAudioProvider._fetch_wikitext(
                lemma=lemma, language=language, cache=cache, throttles=throttles, stats=stats,
            )
            if wikitext is None:
                return None
            resolutions = {}

        markers = language.media.get('audio_filename_markers', [])
        candidates = WiktionaryAudioProvider._extract_candidates(wikitext=wikitext, markers=markers)
        for filename in candidates:
            resolution = resolutions.get(filename)
            if resolution is None:
                resolution = WiktionaryAudioProvider._resolve_commons_url(filename=filename, throttles=throttles, stats=stats)
                if resolution is None:
                    continue
                resolutions[filename] = resolution
                cache.store(
                    provider=WiktionaryAudioProvider.NAME, identifier=lemma,
                    payload={'found': True, 'wikitext': wikitext, 'resolutions': resolutions},
                )
            return filename, resolution['url']
        return None

    @staticmethod
    def _fetch_wikitext(
        *, lemma: str, language: language_config.LanguageConfig, cache: Cache,
        throttles: HostThrottleRegistry, stats: Stats,
    ) -> typing.Optional[str]:
        """Returns the page's wikitext, caching found/not-found; a network failure caches nothing."""
        url = WIKTIONARY_PARSE_URL_TEMPLATE.format(host=language.wiktionary_host, lemma=urllib.parse.quote(lemma))
        response = HttpClient.get_text(url=url, throttle=throttles.for_host(host=language.wiktionary_host), stats=stats)
        if response.status == FetchStatus.FAILED:
            return None
        if response.status == FetchStatus.NOT_FOUND or response.text is None:
            cache.store(provider=WiktionaryAudioProvider.NAME, identifier=lemma, payload={'found': False})
            return None
        body = json.loads(response.text)
        # A missing page is a normal 200 response carrying an "error" key, not an HTTP 404.
        if 'error' in body:
            cache.store(provider=WiktionaryAudioProvider.NAME, identifier=lemma, payload={'found': False})
            return None
        wikitext = body.get('parse', {}).get('wikitext', '')
        cache.store(
            provider=WiktionaryAudioProvider.NAME, identifier=lemma,
            payload={'found': True, 'wikitext': wikitext, 'resolutions': {}},
        )
        return wikitext

    @staticmethod
    def _extract_candidates(*, wikitext: str, markers: typing.Sequence[str]) -> typing.List[str]:
        """Filenames from either markup: a pronunciation table's audio parameters, or a plain audio template."""
        values = []
        for match in PRON_GRAF_TEMPLATE_PATTERN.finditer(wikitext):
            for part in (match.group(1) or '').split('|'):
                if '=' not in part:
                    continue
                name, value = (segment.strip() for segment in part.split('=', 1))
                if AUDIO_PARAM_NAME_PATTERN.match(name):
                    values.append(value)
        for match in AUDIO_TEMPLATE_PATTERN.finditer(wikitext):
            values.extend(part.strip() for part in match.group(1).split('|'))
        return [
            value for value in dict.fromkeys(values)
            if value.lower().endswith(AUDIO_FILENAME_SUFFIXES)
            and (not markers or any(marker in value for marker in markers))
        ]

    @staticmethod
    def _resolve_commons_url(
        *, filename: str, throttles: HostThrottleRegistry, stats: Stats,
    ) -> typing.Optional[dict]:
        url = COMMONS_IMAGEINFO_URL_TEMPLATE.format(filename=urllib.parse.quote(filename))
        response = HttpClient.get_text(url=url, throttle=throttles.for_host(host=COMMONS_HOST), stats=stats)
        if response.status != FetchStatus.FOUND or not response.text:
            return None
        body = json.loads(response.text)
        for page in body.get('query', {}).get('pages', {}).values():
            imageinfo = page.get('imageinfo')
            if imageinfo:
                info = imageinfo[0]
                return {'url': info.get('url', ''), 'mime': info.get('mime', ''), 'size': info.get('size', 0)}
        return None


class OpenverseImageProvider:
    """Searches Openverse with the row's pivot-language translation; no API key required."""

    NAME = ImageProviderName.OPENVERSE

    @staticmethod
    def fetch(*, query: str, cache: Cache, throttles: HostThrottleRegistry, stats: Stats) -> typing.Optional[ImageResult]:
        cached = cache.load(provider=OpenverseImageProvider.NAME, identifier=query)
        if cached is not None:
            if not cached.get('found'):
                return None
            results = cached.get('results', [])
        else:
            url = OPENVERSE_SEARCH_URL_TEMPLATE.format(query=urllib.parse.quote(query), page_size=OPENVERSE_PAGE_SIZE)
            response = HttpClient.get_text(url=url, throttle=throttles.for_host(host=OPENVERSE_HOST), stats=stats)
            if response.status == FetchStatus.FAILED:
                return None
            if response.status == FetchStatus.NOT_FOUND or not response.text:
                cache.store(provider=OpenverseImageProvider.NAME, identifier=query, payload={'found': False})
                return None
            body = json.loads(response.text)
            results = body.get('results') or []
            cache.store(
                provider=OpenverseImageProvider.NAME, identifier=query, payload={'found': bool(results), 'results': results},
            )
            if not results:
                return None

        for result in results:
            image_url = result.get('url')
            if not image_url:
                continue
            downloaded = MediaDownloader.fetch_and_validate_image(url=image_url, throttles=throttles, stats=stats)
            if downloaded is None:
                continue
            content, extension = downloaded
            return ImageResult(
                content=content,
                extension=extension,
                source=OpenverseImageProvider.NAME,
                license=result.get('license') or '',
                attribution=OpenverseImageProvider.attribution(result=result),
            )
        return None

    @staticmethod
    def attribution(*, result: dict) -> str:
        creator = (result.get('creator') or '').strip()
        landing_url = (result.get('foreign_landing_url') or '').strip()
        return ' — '.join(part for part in (creator, landing_url) if part)


class WikipediaImageProvider:
    """Last resort: whatever image Wikipedia's own infobox happens to show for the lemma."""

    NAME = ImageProviderName.WIKIPEDIA

    @staticmethod
    def fetch(
        *, lemma: str, language: language_config.LanguageConfig, cache: Cache,
        throttles: HostThrottleRegistry, stats: Stats,
    ) -> typing.Optional[ImageResult]:
        page_image = WikipediaImageProvider._page_image(
            lemma=lemma, language=language, cache=cache, throttles=throttles, stats=stats,
        )
        if page_image is None:
            return None
        licence = WikipediaImageProvider._file_licence(
            filename=page_image['name'], language=language, cache=cache, throttles=throttles, stats=stats,
        )
        # A non-free file is used on Wikipedia under fair use, which does not reach a flashcard.
        if licence is None or licence['non_free']:
            return None
        downloaded = MediaDownloader.fetch_and_validate_image(url=page_image['url'], throttles=throttles, stats=stats)
        if downloaded is None:
            return None
        content, extension = downloaded
        return ImageResult(
            content=content, extension=extension, source=WikipediaImageProvider.NAME,
            license=licence['license'], attribution=licence['attribution'],
        )

    @staticmethod
    def _page_image(
        *, lemma: str, language: language_config.LanguageConfig, cache: Cache,
        throttles: HostThrottleRegistry, stats: Stats,
    ) -> typing.Optional[dict]:
        """The article's lead image as its URL and file name."""
        cached = cache.load(provider=WikipediaImageProvider.NAME, identifier=lemma)
        # An entry cached before file names were kept has no name to look the licence up by.
        if cached is not None and (not cached.get('found') or cached.get('name')):
            return {'url': cached['url'], 'name': cached['name']} if cached.get('found') else None
        url = WIKIPEDIA_PAGEIMAGE_URL_TEMPLATE.format(host=language.wikipedia_host, title=urllib.parse.quote(lemma))
        response = HttpClient.get_text(url=url, throttle=throttles.for_host(host=language.wikipedia_host), stats=stats)
        if response.status == FetchStatus.FAILED:
            return None
        if response.status == FetchStatus.NOT_FOUND or not response.text:
            cache.store(provider=WikipediaImageProvider.NAME, identifier=lemma, payload={'found': False})
            return None
        page_image = WikipediaImageProvider._extract_page_image(body=json.loads(response.text))
        cache.store(
            provider=WikipediaImageProvider.NAME, identifier=lemma,
            payload={'found': page_image is not None, **(page_image or {})},
        )
        return page_image

    @staticmethod
    def _extract_page_image(*, body: dict) -> typing.Optional[dict]:
        for page in body.get('query', {}).get('pages', {}).values():
            original = page.get('original')
            if original and original.get('source') and page.get('pageimage'):
                return {'url': original['source'], 'name': page['pageimage']}
        return None

    @staticmethod
    def _file_licence(
        *, filename: str, language: language_config.LanguageConfig, cache: Cache,
        throttles: HostThrottleRegistry, stats: Stats,
    ) -> typing.Optional[dict]:
        """The file's licence, author and free status; None when the wiki could not be asked."""
        identifier = f'{WIKIPEDIA_FILE_CACHE_PREFIX}{filename}'
        cached = cache.load(provider=WikipediaImageProvider.NAME, identifier=identifier)
        if cached is not None:
            return cached
        url = WIKIPEDIA_FILE_LICENCE_URL_TEMPLATE.format(host=language.wikipedia_host, filename=urllib.parse.quote(filename))
        response = HttpClient.get_text(url=url, throttle=throttles.for_host(host=language.wikipedia_host), stats=stats)
        if response.status != FetchStatus.FOUND or not response.text:
            return None
        licence = WikipediaImageProvider._extract_licence(body=json.loads(response.text))
        cache.store(provider=WikipediaImageProvider.NAME, identifier=identifier, payload=licence)
        return licence

    @staticmethod
    def _extract_licence(*, body: dict) -> dict:
        """An unknown licence counts as non-free, so only a file with a stated licence lands."""
        for page in body.get('query', {}).get('pages', {}).values():
            for info in page.get('imageinfo') or []:
                metadata = info.get('extmetadata') or {}
                licence = WikipediaImageProvider._metadata_text(metadata=metadata, field='LicenseShortName')
                artist = WikipediaImageProvider._metadata_text(metadata=metadata, field='Artist')
                landing_url = (info.get('descriptionurl') or '').strip()
                non_free = WikipediaImageProvider._metadata_text(metadata=metadata, field='NonFree').lower() in ('true', '1')
                return {
                    'license': licence,
                    'attribution': ' — '.join(part for part in (artist, landing_url) if part),
                    'non_free': non_free or not licence,
                }
        return {'license': '', 'attribution': '', 'non_free': True}

    @staticmethod
    def _metadata_text(*, metadata: dict, field: str) -> str:
        """A metadata value as plain text; the wiki returns some of them as HTML."""
        value = str((metadata.get(field) or {}).get('value') or '')
        return re.sub(r'\s+', ' ', html.unescape(HTML_TAG_PATTERN.sub('', value))).strip()


class ManifestStore:
    """Loads media.tsv, fills only the cells a lookup found empty, and rewrites the merged file."""

    def __init__(self, *, path: pathlib.Path, entries: typing.Dict[str, typing.Dict[str, str]]) -> None:
        self._path = path
        self._entries = entries

    @classmethod
    def load(cls, *, path: pathlib.Path) -> 'ManifestStore':
        if not path.exists():
            return cls(path=path, entries={})
        entries = {row['key']: dict(row) for row in language_config.TsvFile.read(path)}
        return cls(path=path, entries=entries)

    def is_filled(self, *, key: str, column: str) -> bool:
        return bool(self._entries.get(key, {}).get(column))

    def get(self, *, key: str, column: str) -> str:
        return self._entries.get(key, {}).get(column, '')

    def set(self, *, key: str, column: str, value: str) -> None:
        """Callers only ever call this after is_filled said the cell was empty."""
        self._entries.setdefault(key, {'key': key})[column] = value

    def write(self, *, columns: typing.Sequence[str]) -> None:
        language_config.TsvFile.write(self._path, rows=self._entries.values(), columns=columns)


class RowFields:
    """Pulls the handful of row values media lookups need, tolerating missing columns."""

    @staticmethod
    def lemma(*, row: typing.Dict[str, str]) -> str:
        # The bare dictionary form lives before any comma; the key itself carries the
        # sense-disambiguating gloss and must never be looked up against an external API.
        return row.get('word', '').split(',', 1)[0].strip()

    @staticmethod
    def is_object(*, row: typing.Dict[str, str]) -> bool:
        return row.get('is_object', '').strip().lower() == 'yes'

    @staticmethod
    def pivot_translation(*, row: typing.Dict[str, str], language: language_config.LanguageConfig) -> str:
        if language.pivot is None:
            return ''
        column = f'translations_{language.pivot}'
        return row.get(column, '').split(',', 1)[0].strip()

    @classmethod
    def search_term(cls, *, row: typing.Dict[str, str], language: language_config.LanguageConfig) -> str:
        """What to type into an image search for this word.

        Picture archives are captioned in English, so the English translation is the query. When
        the language being learned is itself English there is no such translation to hold it —
        the headword is already the query — and without this fallback the search is handed an
        empty string and every picture is skipped in silence.
        """
        return cls.pivot_translation(row=row, language=language) or cls.lemma(row=row)


class MediaFetcher:
    """Coordinates provider lookups and materialises whatever they find under stable filenames."""

    def __init__(self, *, language: language_config.LanguageConfig, data_directory: pathlib.Path, stats: Stats) -> None:
        self._language = language
        self._data_directory = data_directory
        self._catalogue = None
        self._media_directory = data_directory / MEDIA_DIRECTORY_NAME
        self._media_directory.mkdir(parents=True, exist_ok=True)
        self._cache = Cache(root=data_directory / CACHE_DIRECTORY_NAME)
        self._throttles = HostThrottleRegistry(
            max_workers_per_host=MAX_WORKERS_PER_HOST, min_interval_seconds=HOST_MIN_REQUEST_INTERVAL_SECONDS,
        )
        self._stats = stats

    def slug_for(self, *, key: str) -> str:
        return reword_media.Slugger.slug(key=key, language_code=self._language.target)

    def fetch_audio(self, *, rows: typing.List[dict], manifest: ManifestStore) -> None:
        providers = [
            name for name in self._language.media.get('audio_providers', [])
            if name != AudioProviderName.SOURCE_BACKUP
        ]
        pending = [row for row in rows if not manifest.is_filled(key=row['key'], column='audio')]
        if not providers or not pending:
            return

        # Several senses of one headword share a recording, so resolve per lemma, not per row.
        lemma_by_key = {row['key']: RowFields.lemma(row=row) for row in pending}
        rows_by_lemma: typing.Dict[str, typing.List[dict]] = {}
        for row in pending:
            rows_by_lemma.setdefault(lemma_by_key[row['key']], []).append(row)

        with concurrent.futures.ThreadPoolExecutor(max_workers=EXECUTOR_MAX_WORKERS) as executor:
            futures = {
                executor.submit(self._resolve_audio, lemma=lemma, providers=providers): lemma
                for lemma in rows_by_lemma
            }
            for done, future in enumerate(concurrent.futures.as_completed(futures), start=1):
                resolved = future.result()
                if resolved is not None:
                    for row in rows_by_lemma[futures[future]]:
                        self._store_audio(row=row, resolved=resolved, manifest=manifest)
                # A slow source can make this run for hours, so what is already fetched is
                # written as it arrives; stopping the run then costs nothing but the rest.
                if done % MANIFEST_FLUSH_EVERY == 0:
                    manifest.write(columns=language_config.MEDIA_MANIFEST_COLUMNS)
                    print(f"  {done}/{len(futures)} lemmas, {self._stats.audio_fetched} recordings", file=sys.stderr)
        manifest.write(columns=language_config.MEDIA_MANIFEST_COLUMNS)

    def _store_audio(self, *, row: dict, resolved: typing.Tuple[bytes, str, str], manifest: 'ManifestStore') -> None:
        content, extension, source = resolved
        target_path = self._media_directory / f"{self.slug_for(key=row['key'])}.{extension}"
        if not target_path.exists():
            target_path.write_bytes(content)
            target_path = PortableMedia.audio(path=target_path)
        manifest.set(key=row['key'], column='audio', value=target_path.name)
        manifest.set(key=row['key'], column='audio_source', value=source)
        self._stats.audio_fetched += 1

    @property
    def _picture_dictionary(self):
        """Imported here because that module builds on this one; importing it at the top would
        close the loop. Loaded once and remembered."""
        if self._catalogue is None:
            from . import picture_dictionary
            self._catalogue = picture_dictionary.Catalogue.load(
                language=self._language, data_directory=self._data_directory,
            )
        return self._catalogue

    def _resolve_audio(
        self, *, lemma: str, providers: typing.List[str],
    ) -> typing.Optional[typing.Tuple[bytes, str, str]]:
        for provider_name in providers:
            if provider_name == AudioProviderName.PICTURE_DICTIONARY:
                from . import picture_dictionary
                resolved = picture_dictionary.PictureDictionaryProvider.audio(
                    lemma=lemma, catalogue=self._picture_dictionary,
                    throttles=self._throttles, stats=self._stats,
                )
            elif provider_name == AudioProviderName.WIKTIONARY:
                resolved = self._download_wiktionary_audio(lemma=lemma)
            elif provider_name == AudioProviderName.DICTIONARY:
                resolved = DictionaryAudioProvider.fetch(
                    lemma=lemma, language=self._language, throttles=self._throttles, stats=self._stats,
                )
            elif provider_name == AudioProviderName.SYNTHESIS:
                resolved = SynthesisAudioProvider.fetch(
                    lemma=lemma, language=self._language, throttles=self._throttles, stats=self._stats,
                )
            else:
                raise ValueError(f"unknown audio provider {provider_name!r} in language config")
            if resolved is not None:
                return (*resolved, provider_name)
        return None

    def _download_wiktionary_audio(self, *, lemma: str) -> typing.Optional[typing.Tuple[bytes, str]]:
        resolved = WiktionaryAudioProvider.resolve(
            lemma=lemma, language=self._language, cache=self._cache, throttles=self._throttles, stats=self._stats,
        )
        if resolved is None:
            return None
        filename, url = resolved
        extension = filename.rsplit('.', 1)[-1].lower()
        content = MediaDownloader.fetch_and_validate_audio(
            url=url, extension=extension, throttles=self._throttles, stats=self._stats,
        )
        return None if content is None else (content, extension)

    def fetch_images(self, *, rows: typing.List[dict], manifest: ManifestStore, has_cards: bool) -> None:
        if not has_cards:
            print("no cards.tsv found — skipping images, only words.tsv columns are available", file=sys.stderr)
            return

        providers = [
            name for name in self._language.media.get('image_providers', [])
            if name != ImageProviderName.SOURCE_BACKUP
        ]
        pending = [
            row for row in rows
            if RowFields.is_object(row=row) and not manifest.is_filled(key=row['key'], column='image')
        ]
        if not pending:
            return

        results: typing.Dict[str, typing.Optional[ImageResult]] = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=EXECUTOR_MAX_WORKERS) as executor:
            futures = {
                executor.submit(self._resolve_image, row=row, providers=providers): row['key'] for row in pending
            }
            for future in concurrent.futures.as_completed(futures):
                results[futures[future]] = future.result()

        for row in pending:
            result = results.get(row['key'])
            if result is None:
                continue
            target_path = self._media_directory / f"{self.slug_for(key=row['key'])}.{result.extension}"
            if not target_path.exists():
                target_path.write_bytes(result.content)
                PortableMedia.image(path=target_path)
            manifest.set(key=row['key'], column='image', value=target_path.name)
            manifest.set(key=row['key'], column='image_source', value=result.source)
            manifest.set(key=row['key'], column='image_license', value=result.license)
            manifest.set(key=row['key'], column='image_attribution', value=result.attribution)
            self._stats.images_fetched += 1

    def _resolve_image(self, *, row: dict, providers: typing.List[str]) -> typing.Optional[ImageResult]:
        for provider_name in providers:
            if provider_name == ImageProviderName.PICTURE_DICTIONARY:
                from . import picture_dictionary
                result = picture_dictionary.PictureDictionaryProvider.image(
                    lemma=RowFields.lemma(row=row), catalogue=self._picture_dictionary,
                    throttles=self._throttles, stats=self._stats,
                )
            elif provider_name == ImageProviderName.OPENVERSE:
                query = RowFields.search_term(row=row, language=self._language)
                if not query:
                    continue  # nothing to search with; try the next provider
                result = OpenverseImageProvider.fetch(query=query, cache=self._cache, throttles=self._throttles, stats=self._stats)
            elif provider_name == ImageProviderName.WIKIPEDIA:
                result = WikipediaImageProvider.fetch(
                    lemma=RowFields.lemma(row=row), language=self._language,
                    cache=self._cache, throttles=self._throttles, stats=self._stats,
                )
            else:
                raise ValueError(f"unknown image provider {provider_name!r} in language config")
            if result is not None:
                return result
        return None


def main_for(
    *,
    language: language_config.LanguageConfig,
    root: pathlib.Path,
    limit: typing.Optional[int] = None,
    only: typing.Optional[str] = None,
) -> None:
    data_directory = language.data_directory(root=root)

    cards_path = data_directory / CARDS_FILENAME
    has_cards = cards_path.exists()
    input_path = cards_path if has_cards else data_directory / WORDS_FILENAME
    rows = language_config.TsvFile.read(input_path)
    if limit is not None:
        rows = rows[:limit]
    if not has_cards:
        print("cards.tsv not found — fetching audio only, images need is_object and pivot translations", file=sys.stderr)

    slugs_by_key = {row['key']: reword_media.Slugger.slug(key=row['key'], language_code=language.target) for row in rows}
    reword_media.Slugger.assert_unique(slugs_by_key=slugs_by_key)

    manifest = ManifestStore.load(path=data_directory / MANIFEST_FILENAME)
    stats = Stats()
    fetcher = MediaFetcher(language=language, data_directory=data_directory, stats=stats)

    if only != 'image':
        fetcher.fetch_audio(rows=rows, manifest=manifest)
    if only != 'audio':
        fetcher.fetch_images(rows=rows, manifest=manifest, has_cards=has_cards)

    manifest.write(columns=language_config.MEDIA_MANIFEST_COLUMNS)
    print(stats.summary(total_rows=len(rows)))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--limit', type=int, default=None, help="Process only the first N rows")
    parser.add_argument('--only', choices=('audio', 'image'), default=None, help="Fetch only one kind of media")
    language_config.add_language_arguments(parser)
    arguments = parser.parse_args()

    root = language_config.PROJECT_ROOT
    language = language_config.language_from(arguments, root=root)
    main_for(language=language, root=root, limit=arguments.limit, only=arguments.only)


if __name__ == '__main__':
    main()
