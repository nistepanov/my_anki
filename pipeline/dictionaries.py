"""Enrich the TSV produced by the source-list export with dictionary data.

Fetches definitions, synonyms, antonyms (a target-language dictionary, with its Wiktionary edition
as a fallback/supplement) and, where the source list had no example sentences, short A1-A2 example
sentences from Tatoeba. Purely deterministic: no LLM involved, everything is cached on disk so
re-runs are free.

Column names and API hosts come from a per-language config under languages/, so the pipeline
never hardcodes a language. The actual dictionary integration (RAE) is Spanish-specific and the
only backend implemented so far; other target languages need their own client.

An edition that marks a synonym or antonym with an inline template states it under the sense it
belongs to, not the entry as a whole, so this stage keeps each sense's own words instead of
pooling every sense's list onto every card built from the entry. An edition that only files
related words under a named block has no sense to attach them to, so those stay entry-wide as
before. A related word tagged rare, obsolete, archaic, dated or nonstandard is read for that tag
rather than stripped of it, so the tag can drop the word instead of teaching it as current.
"""

import argparse
import bisect
import concurrent.futures
import enum
import functools
import json
import pathlib
import random
import re
import sys
import threading
import time
import typing

import unicodedata
import urllib.error
import urllib.parse
import urllib.request

from . import frequency
from . import language_config

USER_AGENT = 'my-anki-export/1.0 (personal Anki deck build)'
REQUEST_TIMEOUT_SECONDS = 10.0
RETRY_ATTEMPTS = 4  # one attempt plus three retries
RETRY_BACKOFF_BASE_SECONDS = 1.0
RETRY_JITTER_MAX_SECONDS = 0.5

HTTP_STATUS_NOT_FOUND = 404
HTTP_STATUS_TOO_MANY_REQUESTS = 429

RAE_MAX_WORKERS = 2
# A batch request replaces up to WIKTIONARY_BATCH_SIZE individual ones, so the run needs far
# fewer requests overall; the server paces us regardless of thread count, so this stays low.
WIKTIONARY_MAX_WORKERS = 2
TATOEBA_MAX_WORKERS = 3
# MediaWiki's own limit on titles per query for anonymous (unauthenticated) clients.
WIKTIONARY_BATCH_SIZE = 50
# Recorded beside cached wikitext, since only the edition that wrote it can be parsed back.
EDITION_KEY = 'edition'

# RAE's own free-tier response headers report a hard 10-requests-per-minute ceiling (and a
# separate 100-per-day one); a quarter second between requests still bursts well past that, so
# the interval is set from the observed limit (60s / 10) with a small safety margin instead.
RAE_MIN_REQUEST_INTERVAL_SECONDS = 6.5
WIKTIONARY_MIN_REQUEST_INTERVAL_SECONDS = 0.25
# Caps how long a single retry actually sleeps: RAE's daily cap, once exhausted, reports a
# multi-hour Retry-After, and honouring that literally would stall the whole batch for hours
# instead of failing the request and letting the run finish.
MAX_RETRY_DELAY_SECONDS = 30.0
# A Retry-After beyond this means a spent quota, not congestion. Waiting it out would
# stall the run for hours, so the source is dropped for the rest of the run instead.
QUOTA_EXHAUSTED_RETRY_AFTER_SECONDS = 300.0

RAE_DICTIONARY_NAME = 'rae'
RAE_API_URL_TEMPLATE = 'https://rae-api.com/api/words/{lemma}'
WIKTIONARY_BATCH_API_URL_TEMPLATE = (
    'https://{host}/w/api.php?action=query&prop=revisions&rvprop=content&rvslots=main'
    '&format=json&formatversion=2&redirects=1&titles={titles}'
)
TATOEBA_API_URL_TEMPLATE = (
    'https://tatoeba.org/en/api_v0/search?from={corpus_language_code}&query={lemma}&limit={limit}'
)
TATOEBA_SEARCH_LIMIT = 20
TATOEBA_MIN_WORDS = 4
TATOEBA_MAX_WORDS = 10
TATOEBA_MAX_EXAMPLES = 3

TATOEBA_RELEVANCE_MIN_LEMMA_LENGTH = 4
TATOEBA_RELEVANCE_MAX_LENGTH_SURPLUS = 4
TATOEBA_RELEVANCE_PREFIX_DROP_ALLOWANCE = 3

RAE_MAX_EXAMPLES = 3
RELATED_WORDS_LIMIT = 3
# How much rarer than the headword a synonym may be before it stops explaining anything. One step
# on wordfreq's scale is a tenfold difference in how often the word turns up.
RELATED_WORD_RARITY_MARGIN = 1.0

# A raw definition line still carries the markup around it: a leading marker means it is a
# quotation or usage example rather than a sense, a leading or embedded template means the text
# never finished expanding, and a leading comma or semicolon means the sense's own lead-in was a
# template that got stripped, leaving a fragment.
SENSE_MARKER_PREFIX_PATTERN = re.compile(r'^[#*:]')
SENSE_TEMPLATE_START_PATTERN = re.compile(r'^\s*\{\{')
SENSE_LEAD_IN_PATTERN = re.compile(r'^\s*[,;]')
SENSE_MINIMUM_LENGTH = 12
SENSE_LIMIT = 25

CACHE_DIRECTORY_NAME = 'cache'
DEFAULT_INPUT_FILENAME = 'words.tsv'
DEFAULT_OUTPUT_FILENAME = 'enriched.tsv'
DICTIONARY_EXAMPLES_COLUMN = 'dictionary_examples'
DEFINITION_SOURCE_COLUMN = 'definition_source'
EXAMPLES_SOURCE_COLUMN = 'examples_source'

SECTION_HEADING_PATTERN = re.compile(r'^==[^=].*==\s*$', re.MULTILINE)
WIKILINK_PATTERN = re.compile(r'\[\[([^\]|]*)(?:\|([^\]]*))?\]\]')
TEMPLATE_PATTERN = re.compile(r'\{\{([^{}|]*)((?:\|[^{}]*)?)\}\}')
# How deep a nest of templates is unwrapped before giving up. Quotation markup reaches three
# levels in practice; the cap only stops a malformed page from looping.
TEMPLATE_NESTING_LIMIT = 8
REFERENCE_TAG_PATTERN = re.compile(r'<ref[^>]*?/>|<ref[^>]*?>.*?</ref>', re.DOTALL)
EMPHASIS_PATTERN = re.compile(r"'{2,3}")
# An edition that files related words by block puts the sense's register and usage note in the
# same list, in italics, and links the words in it. Read as entries they become synonyms that
# teach "colloquial" and "derogatory" instead of a word.
ITALIC_SPAN_PATTERN = re.compile(r"''.+?''")
WHITESPACE_PATTERN = re.compile(r'\s+')
TRAILING_DIGIT_SUFFIX_PATTERN = re.compile(r'^(\D{2,})\d+$')
WORD_TOKEN_PATTERN = re.compile(r'[^\W\d_]+')

LookupResultT = typing.TypeVar('LookupResultT')


class BlockMarkers(typing.NamedTuple):
    """Which named block holds which kind of content, for an edition that files by block.

    Some editions write every list in an entry with the same line syntax and say what the list
    is only in the heading above it, so a sense, an example, an etymology and an idiom are
    indistinguishable line by line. Reading such an edition line by line collects all of them
    and calls them senses.
    """

    boundary: typing.Pattern
    definitions: str
    synonyms: str
    antonyms: str


class WikitextDialect(typing.NamedTuple):
    """How one Wiktionary edition writes the parts of an entry this stage reads.

    Below the page title the editions agree on almost nothing: where a language's section
    starts, how a sense is marked, which template names carry a synonym or point at a base
    form. Reading one edition's markup with another's patterns finds nothing whatsoever and
    reports no error, so the edition has to bring its own patterns rather than inherit them.
    """

    language_section: typing.Pattern
    definition_line: typing.Pattern
    # None where the edition's section titles are written in its own language rather than in the
    # vocabulary a row's part_of_speech uses; a sense then states no part of speech this stage can
    # read, and choosing by it would be guesswork dressed up as a match.
    part_of_speech_heading: typing.Optional[typing.Pattern]
    related_word: typing.Pattern
    synonym_template_names: typing.FrozenSet[str]
    form_pointer: typing.Pattern
    # Templates that render one of their arguments as running text; dropping them whole beheads
    # the definition instead of tidying it.
    templates_rendering_argument: typing.FrozenSet[str]
    # Set only for an edition whose blocks, not whose line syntax, say what a list holds.
    blocks: typing.Optional[BlockMarkers] = None

    @staticmethod
    def spanish_edition(*, language: language_config.LanguageConfig) -> 'WikitextDialect':
        return WikitextDialect(
            language_section=re.compile(
                r'^==\s*\{\{lengua\|' + re.escape(language.code) + r'\}\}\s*==\s*$', re.MULTILINE,
            ),
            definition_line=re.compile(r'^;\d+\s*(?:\{\{[^{}]*\}\}\s*)*:\s*(.*)$', re.MULTILINE),
            part_of_speech_heading=None,
            related_word=re.compile(r'\{\{(sinónimo|antónimo)\|([^{}]*)\}\}'),
            synonym_template_names=frozenset({'sinónimo'}),
            form_pointer=re.compile(r'\{\{\s*(forma\b[^{}|]*)\|([^{}]*)\}\}'),
            templates_rendering_argument=frozenset({'plm', 'l', 'w', 'préstamo', 'variante'}),
        )

    @staticmethod
    def english_edition(*, language: language_config.LanguageConfig) -> 'WikitextDialect':
        code = re.escape(language.code)
        return WikitextDialect(
            language_section=re.compile(
                r'^==\s*' + re.escape(language.target_name) + r'\s*==\s*$', re.MULTILINE,
            ),
            # A sense is a run of hashes; a hash followed by punctuation introduces the quotations
            # and usage examples hanging off the sense above it. Sub-senses count as senses: an entry
            # often spends its top level on a bare grammar label and states the meanings one level in,
            # and reading only the top level then finds nothing at all. The lookahead also refuses a
            # hash, or the run backs off one character and reads a sub-sense's quotation as a sense.
            definition_line=re.compile(r'^#+(?![#:*])\s*(.*)$', re.MULTILINE),
            part_of_speech_heading=re.compile(r'^===+\s*([^=\n]+?)\s*===+\s*$', re.MULTILINE),
            related_word=re.compile(r'\{\{(syn|ant)\|' + code + r'\|([^{}]*)\}\}'),
            synonym_template_names=frozenset({'syn'}),
            form_pointer=re.compile(r'\{\{\s*([a-z][a-z ]*\bof)\|([^{}]*)\}\}'),
            templates_rendering_argument=frozenset({'l', 'm', 'w', 'll', 'mention'}),
        )

    @staticmethod
    def russian_edition(*, language: language_config.LanguageConfig) -> 'WikitextDialect':
        return WikitextDialect(
            # One equals sign, and the language named by a template rather than spelled out.
            language_section=re.compile(
                r'^=\s*\{\{-' + re.escape(language.code) + r'-\}\}\s*=\s*$', re.MULTILINE,
            ),
            # Senses, synonyms, antonyms, hypernyms and idioms are all hash lists, so the heading
            # above a list is the only thing that says which one it is.
            definition_line=re.compile(r'^#(?![#:*])\s*(.*)$', re.MULTILINE),
            part_of_speech_heading=None,
            related_word=WIKILINK_PATTERN,
            synonym_template_names=frozenset(),
            form_pointer=re.compile(r'\{\{\s*(форма-[^{}|]*)\|([^{}]*)\}\}'),
            # Everything else is dropped whole, which is what the example and emphasis templates
            # wrapping a quotation need.
            templates_rendering_argument=frozenset(),
            blocks=BlockMarkers(
                boundary=re.compile(r'^={3,}\s*([^=\n]+?)\s*={3,}\s*$', re.MULTILINE),
                definitions='Значение',
                synonyms='Синонимы',
                antonyms='Антонимы',
            ),
        )

    @staticmethod
    def german_edition(*, language: language_config.LanguageConfig) -> 'WikitextDialect':
        return WikitextDialect(
            language_section=re.compile(
                r'^==[^=\n]*\(\{\{Sprache\|' + re.escape(language.wiktionary_section) + r'\}\}\)\s*==\s*$',
                re.MULTILINE,
            ),
            # Every list in the entry is written as a sense number in brackets followed by the
            # content, which is why the block above it has to decide what was matched.
            definition_line=re.compile(r'^:\s*\[[^\]\n]*\]\s*(.*)$', re.MULTILINE),
            part_of_speech_heading=None,
            # Unused under block markers: the block, not the template, names the relation.
            related_word=WIKILINK_PATTERN,
            synonym_template_names=frozenset(),
            form_pointer=re.compile(r'\{\{\s*(Grundformverweis[^{}|]*)\|([^{}]*)\}\}'),
            templates_rendering_argument=frozenset({'ü', 'üt', 'l', 'w'}),
            blocks=BlockMarkers(
                boundary=re.compile(r'^\{\{([^{}|\n]+)\}\}\s*$', re.MULTILINE),
                definitions='Bedeutungen',
                synonyms='Synonyme',
                antonyms='Gegenwörter',
            ),
        )


# Keyed by the edition being read, which is the subdomain of its host, not by the deck's
# language — a deck could sensibly read an edition written in some third language.
WIKITEXT_DIALECTS = {
    'es': WikitextDialect.spanish_edition,
    'en': WikitextDialect.english_edition,
    'de': WikitextDialect.german_edition,
    'ru': WikitextDialect.russian_edition,
}


@functools.lru_cache(maxsize=None)
def dialect_for(*, host: str, language: language_config.LanguageConfig) -> WikitextDialect:
    edition = host.split('.', 1)[0]
    build = WIKITEXT_DIALECTS.get(edition)
    if build is None:
        raise SystemExit(
            f"no wikitext dialect for the {edition} Wiktionary — its markup has to be described "
            f"before this stage can read it, or every entry silently comes back empty"
        )
    return build(language=language)


class Source(enum.StrEnum):
    RAE = 'rae'
    WIKTIONARY = 'wiktionary'
    TATOEBA = 'tatoeba'


class FetchStatus(enum.StrEnum):
    FOUND = 'found'
    NOT_FOUND = 'not_found'
    FAILED = 'failed'
    EXHAUSTED = 'exhausted'


class DefinitionSource(enum.StrEnum):
    RAE = 'rae'
    WIKTIONARY = 'wiktionary'


class ExamplesSource(enum.StrEnum):
    INPUT = 'input'
    TATOEBA = 'tatoeba'


class FetchResult(typing.NamedTuple):
    status: FetchStatus
    payload: typing.Optional[str]


class RaeRelatedWord(typing.NamedTuple):
    word: str
    usage: str  # register label such as 'colloquial' or 'vulgar'; empty when unmarked


class RaeSense(typing.NamedTuple):
    category: str
    description: str
    usage: str
    examples: typing.List[str]
    synonyms: typing.List[RaeRelatedWord]
    antonyms: typing.List[RaeRelatedWord]


# A whole-group thesaurus page, not a word: it names the group, and no card can carry it.
THESAURUS_LINK_MARKER = 'Thesaurus:'
# A link aimed at one section of an entry keeps the section in the link text.
SECTION_LINK_MARKER = '#'
# Register and sense modifiers, written inline after the word they qualify.
INLINE_MODIFIER_PATTERN = re.compile(r'<[^<>]*>?')
# A `<q:...>`/`<qq:...>` qualifier, read before INLINE_MODIFIER_PATTERN discards it; its
# comma-separated contents can hold several labels at once (e.g. "dated, dialectal").
RARITY_QUALIFIER_PATTERN = re.compile(r'<qq?:([^<>]*)>')
RARITY_LABELS = frozenset({'rare', 'obsolete', 'archaic', 'dated', 'nonstandard'})
# A sense's own labels, e.g. `{{lb|en|transitive|obsolete}}`; read before cleaning strips them.
SENSE_LABEL_PATTERN = re.compile(r'\{\{(?:lb|lbl|label)\|[^|{}]+\|([^{}]*)\}\}')
# Link and emphasis brackets, left behind wherever an argument was cut through the middle.
WIKI_MARKUP_PATTERN = re.compile(r"\[\[|\]\]|''+")
# Prose introducing a list rather than belonging to it.
LIST_PROSE_PREFIX = 'see'
# Whatever survives the modifier pass still carrying an angle bracket is the far half of a
# modifier whose opening bracket stayed in the previous argument — an annotation, not a word.
ANGLE_BRACKET_PATTERN = re.compile(r'[<>]')
# Anything with no letter at all is punctuation an argument split left standing alone.
LETTER_PATTERN = re.compile(r'[^\W\d_]')


class WiktionaryDefinition(typing.NamedTuple):
    """One sense, and the part of speech whose section states it.

    An entry states every part of speech a spelling has, and the senses of the wrong one describe
    a different word: the noun 'a beak' and the verb 'to beak' share nothing but their letters.

    `synonyms`/`antonyms` hold this sense's own related words where the edition states them per
    sense; empty otherwise, not the entry-wide list — see `WiktionaryResult.related_words_are_per_sense`.
    """

    text: str
    part_of_speech: str
    synonyms: typing.Tuple[str, ...] = ()
    antonyms: typing.Tuple[str, ...] = ()
    # Marked rare, obsolete, archaic or dated by the entry itself.
    is_rare: bool = False


class WiktionaryResult(typing.NamedTuple):
    definitions: typing.List[WiktionaryDefinition]
    synonyms: typing.List[str]
    antonyms: typing.List[str]
    # True for an edition whose related words are read per sense (each WiktionaryDefinition then
    # carries its own), false for one that only states them for the entry as a whole.
    related_words_are_per_sense: bool = False


class TatoebaExample(typing.NamedTuple):
    target: str
    native: str
    pivot: str


class RateLimiter:
    """Spaces consecutive requests to one host at least an interval apart, across the whole pool."""

    def __init__(self, *, min_interval_seconds: float) -> None:
        self._min_interval_seconds = min_interval_seconds
        self._lock = threading.Lock()
        self._next_allowed_time = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            delay = self._next_allowed_time - now
            if delay > 0:
                time.sleep(delay)
                now = time.monotonic()
            self._next_allowed_time = now + self._min_interval_seconds


class Cache:
    """On-disk JSON cache, one file per (source, lemma). Also stores negative results."""

    def __init__(self, *, root: pathlib.Path, refresh: bool, edition: str = '') -> None:
        self._root = root
        self._refresh = refresh
        self._edition = edition

    def load(self, *, source: Source, lemma: str) -> typing.Optional[dict]:
        if self._refresh:
            return None
        path = self._path_for(source=source, lemma=lemma)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            return None
        return None if self._came_from_another_edition(source=source, payload=payload) else payload

    def store(self, *, source: Source, lemma: str, payload: dict) -> None:
        if source is Source.WIKTIONARY and self._edition:
            payload = {**payload, EDITION_KEY: self._edition}
        path = self._path_for(source=source, lemma=lemma)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')

    def _came_from_another_edition(self, *, source: Source, payload: dict) -> bool:
        """Whether stored wikitext was written by an edition whose markup this run cannot read.

        Only the edition that produced an entry has a dialect that parses it, so reusing another
        edition's wikitext yields no definitions at all — and reports success while doing it.
        An entry stored before editions were recorded names none, so it is refetched once.
        """
        if source is not Source.WIKTIONARY or not self._edition:
            return False
        return payload.get(EDITION_KEY) != self._edition

    def _path_for(self, *, source: Source, lemma: str) -> pathlib.Path:
        directory = self._root / source.value
        directory.mkdir(parents=True, exist_ok=True)
        filename = lemma.replace('/', '_') + '.json'
        return directory / filename


class HttpClient:
    """Shared GET-with-retries logic used by every source."""

    _exhausted: typing.Set[str] = set()
    _exhausted_lock = threading.Lock()

    @classmethod
    def is_exhausted(cls, *, host: str) -> bool:
        with cls._exhausted_lock:
            return host in cls._exhausted

    @classmethod
    def exhausted_hosts(cls) -> typing.List[str]:
        with cls._exhausted_lock:
            return sorted(cls._exhausted)

    @classmethod
    def _mark_exhausted(cls, *, host: str, retry_after_seconds: float) -> None:
        with cls._exhausted_lock:
            if host in cls._exhausted:
                return
            cls._exhausted.add(host)
        hours = retry_after_seconds / 3600
        print(f"{host} quota spent, skipping it for this run (resets in ~{hours:.1f}h)", file=sys.stderr)

    @classmethod
    def get(cls, *, url: str, rate_limiter: typing.Optional[RateLimiter] = None) -> FetchResult:
        host = urllib.parse.urlsplit(url).netloc
        if cls.is_exhausted(host=host):
            return FetchResult(status=FetchStatus.EXHAUSTED, payload=None)
        request = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
        for attempt in range(RETRY_ATTEMPTS):
            if rate_limiter is not None:
                rate_limiter.wait()
            retry_after_seconds = None
            try:
                with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                    return FetchResult(status=FetchStatus.FOUND, payload=response.read().decode('utf-8'))
            except urllib.error.HTTPError as error:
                if error.code == HTTP_STATUS_NOT_FOUND:
                    return FetchResult(status=FetchStatus.NOT_FOUND, payload=None)
                if error.code == HTTP_STATUS_TOO_MANY_REQUESTS:
                    retry_after_seconds = HttpClient._parse_retry_after(header_value=error.headers.get('Retry-After'))
                    if retry_after_seconds is not None and retry_after_seconds > QUOTA_EXHAUSTED_RETRY_AFTER_SECONDS:
                        cls._mark_exhausted(host=host, retry_after_seconds=retry_after_seconds)
                        return FetchResult(status=FetchStatus.EXHAUSTED, payload=None)
            except (urllib.error.URLError, OSError):
                pass
            if attempt < RETRY_ATTEMPTS - 1:
                time.sleep(HttpClient._backoff_delay(attempt=attempt, retry_after_seconds=retry_after_seconds))
        return FetchResult(status=FetchStatus.FAILED, payload=None)

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


class RaeClient:
    """Fetches and parses RAE dictionary senses for a lemma."""

    _RATE_LIMITER = RateLimiter(min_interval_seconds=RAE_MIN_REQUEST_INTERVAL_SECONDS)

    @staticmethod
    def fetch_one(*, lemma: str, cache: Cache) -> typing.Tuple[FetchStatus, typing.List[RaeSense]]:
        cached = cache.load(source=Source.RAE, lemma=lemma)
        if cached is not None:
            if not cached.get('found'):
                return FetchStatus.NOT_FOUND, []
            return FetchStatus.FOUND, RaeClient._senses_from_data(data=cached.get('data', {}))

        result = HttpClient.get(url=RaeClient._build_url(lemma=lemma), rate_limiter=RaeClient._RATE_LIMITER)
        if result.status == FetchStatus.FAILED:
            return FetchStatus.FAILED, []
        if result.status == FetchStatus.NOT_FOUND:
            cache.store(source=Source.RAE, lemma=lemma, payload={'found': False})
            return FetchStatus.NOT_FOUND, []

        body = json.loads(result.payload)
        if not body.get('ok', False):
            cache.store(source=Source.RAE, lemma=lemma, payload={'found': False})
            return FetchStatus.NOT_FOUND, []

        data = body.get('data', {})
        cache.store(source=Source.RAE, lemma=lemma, payload={'found': True, 'data': data})
        return FetchStatus.FOUND, RaeClient._senses_from_data(data=data)

    @staticmethod
    def _build_url(*, lemma: str) -> str:
        return RAE_API_URL_TEMPLATE.format(lemma=urllib.parse.quote(lemma))

    @staticmethod
    def _senses_from_data(*, data: dict) -> typing.List[RaeSense]:
        senses = []
        for meaning in data.get('meanings') or []:
            for sense in meaning.get('senses') or []:
                usage = (sense.get('usage') or '').strip()
                senses.append(RaeSense(
                    category=(sense.get('category') or '').strip(),
                    description=(sense.get('description') or '').strip(),
                    usage=usage,
                    examples=[example.strip() for example in sense.get('examples') or [] if example and example.strip()],
                    synonyms=RaeClient._related_words(
                        v2_items=sense.get('synonyms_v2'), plain_words=sense.get('synonyms'), sense_usage=usage,
                    ),
                    antonyms=RaeClient._related_words(
                        v2_items=sense.get('antonyms_v2'), plain_words=sense.get('antonyms'), sense_usage=usage,
                    ),
                ))
        return senses

    @staticmethod
    def _related_words(
        *,
        v2_items: typing.Optional[typing.List[dict]],
        plain_words: typing.Optional[typing.List[str]],
        sense_usage: str,
    ) -> typing.List[RaeRelatedWord]:
        # RAE marks register per synonym/antonym ("synonyms_v2"/"antonyms_v2" carry a "label"),
        # not on the sense as a whole; fall back to the sense's own usage, and to the plain word
        # list on older responses that lack the v2 fields.
        if v2_items:
            pairs = [((item.get('word') or '').strip(), (item.get('label') or '').strip()) for item in v2_items]
        else:
            pairs = [(word.strip(), '') for word in plain_words or [] if word]
        related = []
        for word, label in pairs:
            if not word:
                continue
            related.append(RaeRelatedWord(word=word, usage=label or sense_usage))
        return related


class WiktionaryClient:
    """Fetches the target-language section of Wiktionary pages, in batches, and parses them.

    MediaWiki's `action=query` endpoint returns wikitext for up to WIKTIONARY_BATCH_SIZE titles
    in one request, which is what makes batching worthwhile: one round trip for 50 lemmas instead
    of 50, all still paced by the same rate limiter.
    """

    _RATE_LIMITER = RateLimiter(min_interval_seconds=WIKTIONARY_MIN_REQUEST_INTERVAL_SECONDS)

    @staticmethod
    def fetch_many(
        *, lemmas: typing.List[str], cache: Cache, language: language_config.LanguageConfig, stats: 'Stats',
    ) -> typing.Dict[str, WiktionaryResult]:
        unique_lemmas = list(dict.fromkeys(lemmas))
        if not unique_lemmas:
            return {}

        empty = WiktionaryResult(definitions=[], synonyms=[], antonyms=[])
        statuses, wikitexts = WiktionaryClient._wikitext_for_lemmas(lemmas=unique_lemmas, cache=cache, language=language)

        results: typing.Dict[str, WiktionaryResult] = {}
        parsed_by_lemma: typing.Dict[str, WiktionaryResult] = {}
        redirect_lemma_by_lemma: typing.Dict[str, str] = {}
        for lemma in unique_lemmas:
            wikitext = wikitexts.get(lemma)
            if wikitext is None:
                results[lemma] = empty
                continue
            parsed = WiktionaryClient._parse(wikitext=wikitext, language=language)
            parsed_by_lemma[lemma] = parsed
            if parsed.definitions:
                results[lemma] = parsed
                continue
            # An inflected headword's page is often just a pointer template (e.g. a plural
            # pointing at its singular); follow it once to the base lemma's own page instead of
            # giving up.
            redirect_lemma = WiktionaryClient._find_form_redirect(wikitext=wikitext, language=language)
            if redirect_lemma is None:
                results[lemma] = parsed
            else:
                redirect_lemma_by_lemma[lemma] = redirect_lemma

        if redirect_lemma_by_lemma:
            redirect_targets = list(dict.fromkeys(redirect_lemma_by_lemma.values()))
            redirect_statuses, redirect_wikitexts = WiktionaryClient._wikitext_for_lemmas(
                lemmas=redirect_targets, cache=cache, language=language,
            )
            for lemma, target in redirect_lemma_by_lemma.items():
                if redirect_statuses.get(target) == FetchStatus.FAILED:
                    statuses[lemma] = FetchStatus.FAILED
                    results[lemma] = parsed_by_lemma[lemma]
                    continue
                target_wikitext = redirect_wikitexts.get(target)
                results[lemma] = (
                    empty if target_wikitext is None
                    else WiktionaryClient._parse(wikitext=target_wikitext, language=language)
                )

        stats.failed_requests += sum(1 for status in statuses.values() if status == FetchStatus.FAILED)
        return results

    @staticmethod
    def _wikitext_for_lemmas(
        *, lemmas: typing.List[str], cache: Cache, language: language_config.LanguageConfig,
    ) -> typing.Tuple[typing.Dict[str, FetchStatus], typing.Dict[str, typing.Optional[str]]]:
        """Resolves wikitext for each lemma from the cache, batching an HTTP fetch for the rest."""
        statuses: typing.Dict[str, FetchStatus] = {}
        wikitexts: typing.Dict[str, typing.Optional[str]] = {}
        pending = []
        for lemma in lemmas:
            cached = cache.load(source=Source.WIKTIONARY, lemma=lemma)
            if cached is None:
                pending.append(lemma)
            elif cached.get('found'):
                statuses[lemma] = FetchStatus.FOUND
                wikitexts[lemma] = cached.get('wikitext', '')
            else:
                statuses[lemma] = FetchStatus.NOT_FOUND
                wikitexts[lemma] = None

        batches = [pending[start:start + WIKTIONARY_BATCH_SIZE] for start in range(0, len(pending), WIKTIONARY_BATCH_SIZE)]
        if not batches:
            return statuses, wikitexts

        with concurrent.futures.ThreadPoolExecutor(max_workers=WIKTIONARY_MAX_WORKERS) as executor:
            futures = [
                executor.submit(
                    WiktionaryClient._fetch_batch,
                    batch=batch, cache=cache, language=language, batch_number=index, total_batches=len(batches),
                )
                for index, batch in enumerate(batches, start=1)
            ]
            for future in concurrent.futures.as_completed(futures):
                batch_statuses, batch_wikitexts = future.result()
                statuses.update(batch_statuses)
                wikitexts.update(batch_wikitexts)
        return statuses, wikitexts

    @staticmethod
    def _fetch_batch(
        *, batch: typing.List[str], cache: Cache, language: language_config.LanguageConfig,
        batch_number: int, total_batches: int,
    ) -> typing.Tuple[typing.Dict[str, FetchStatus], typing.Dict[str, typing.Optional[str]]]:
        result = HttpClient.get(
            url=WiktionaryClient._build_batch_url(lemmas=batch, language=language),
            rate_limiter=WiktionaryClient._RATE_LIMITER,
        )
        if result.status in (FetchStatus.FAILED, FetchStatus.EXHAUSTED):
            print(f"[wiktionary] batch {batch_number}/{total_batches}: {len(batch)} lemmas, request failed", file=sys.stderr)
            return {lemma: FetchStatus.FAILED for lemma in batch}, {lemma: None for lemma in batch}

        body = json.loads(result.payload)
        query = body.get('query', {})
        resolved_title_by_lemma = WiktionaryClient._resolve_titles(query=query, requested=batch)
        content_by_title = WiktionaryClient._content_by_title(pages=query.get('pages', []))

        statuses: typing.Dict[str, FetchStatus] = {}
        wikitexts: typing.Dict[str, typing.Optional[str]] = {}
        found_count = 0
        for lemma in batch:
            resolved_title = resolved_title_by_lemma.get(lemma, lemma)
            wikitext = content_by_title.get(resolved_title)
            if wikitext is None:
                statuses[lemma] = FetchStatus.NOT_FOUND
                cache.store(source=Source.WIKTIONARY, lemma=lemma, payload={'found': False})
            else:
                statuses[lemma] = FetchStatus.FOUND
                found_count += 1
                cache.store(source=Source.WIKTIONARY, lemma=lemma, payload={'found': True, 'wikitext': wikitext})
            wikitexts[lemma] = wikitext

        print(f"[wiktionary] batch {batch_number}/{total_batches}: {len(batch)} lemmas, {found_count} found", file=sys.stderr)
        return statuses, wikitexts

    @staticmethod
    def _build_batch_url(*, lemmas: typing.List[str], language: language_config.LanguageConfig) -> str:
        titles = '|'.join(lemmas)
        return WIKTIONARY_BATCH_API_URL_TEMPLATE.format(
            host=language.wiktionary_host, titles=urllib.parse.quote(titles, safe='|'),
        )

    @staticmethod
    def _resolve_titles(*, query: dict, requested: typing.List[str]) -> typing.Dict[str, str]:
        """Maps each requested title to the title MediaWiki actually returned content under.

        `normalized` and `redirects` each rewrite a title once; a redirect can chain through a
        normalization, so the rewrite is followed transitively instead of applied once.
        """
        rewrite_by_from: typing.Dict[str, str] = {}
        for entry in (query.get('normalized') or []) + (query.get('redirects') or []):
            rewrite_by_from[entry['from']] = entry['to']

        resolved_by_requested: typing.Dict[str, str] = {}
        for lemma in requested:
            title = lemma
            visited = {title}
            while title in rewrite_by_from and rewrite_by_from[title] not in visited:
                title = rewrite_by_from[title]
                visited.add(title)
            resolved_by_requested[lemma] = title
        return resolved_by_requested

    @staticmethod
    def _content_by_title(*, pages: typing.List[dict]) -> typing.Dict[str, typing.Optional[str]]:
        content_by_title: typing.Dict[str, typing.Optional[str]] = {}
        for page in pages:
            title = page.get('title', '')
            # A missing page is a normal 200 response with "missing": true and no "revisions" key.
            if page.get('missing'):
                content_by_title[title] = None
                continue
            revisions = page.get('revisions') or []
            content_by_title[title] = revisions[0].get('slots', {}).get('main', {}).get('content', '') if revisions else None
        return content_by_title

    @staticmethod
    def _parse(*, wikitext: str, language: language_config.LanguageConfig) -> WiktionaryResult:
        dialect = dialect_for(host=language.wiktionary_host, language=language)
        section = WiktionaryClient._extract_language_section(wikitext=wikitext, language=language)
        if not section:
            return WiktionaryResult(definitions=[], synonyms=[], antonyms=[])

        # Headings and senses are read against one another by position, because a sense belongs to
        # whichever part-of-speech section it stands under and nothing in the line itself says so.
        headings = (
            [(match.start(), match.group(1)) for match in dialect.part_of_speech_heading.finditer(section)]
            if dialect.part_of_speech_heading is not None else []
        )
        definitions = []
        positions = []
        for offset, text in WiktionaryClient._sense_regions(section=section, dialect=dialect):
            for match in dialect.definition_line.finditer(text):
                cleaned = WiktionaryClient._clean_text(text=match.group(1), dialect=dialect)
                if not cleaned:
                    continue
                heading = ''
                for position, title in headings:
                    if position > offset + match.start():
                        break
                    heading = title
                definitions.append(WiktionaryDefinition(
                    text=cleaned, part_of_speech=heading,
                    is_rare=WiktionaryClient._sense_is_rare(raw=match.group(1)),
                ))
                positions.append(offset + match.start())

        # Only an edition that marks related words with an inline template can say which sense
        # they sit under; one that files them under a named block states them for the entry.
        related_words_are_per_sense = dialect.blocks is None
        if related_words_are_per_sense:
            per_sense_synonyms, per_sense_antonyms = WiktionaryClient._related_words_by_sense(
                section=section, dialect=dialect, definition_positions=positions,
            )
            definitions = [
                definition._replace(synonyms=sense_synonyms, antonyms=sense_antonyms)
                for definition, sense_synonyms, sense_antonyms in zip(definitions, per_sense_synonyms, per_sense_antonyms)
            ]

        synonyms, antonyms = WiktionaryClient._related_words(section=section, dialect=dialect)
        return WiktionaryResult(
            definitions=definitions, synonyms=synonyms, antonyms=antonyms,
            related_words_are_per_sense=related_words_are_per_sense,
        )

    @staticmethod
    def _named_blocks(
        *, section: str, dialect: WikitextDialect,
    ) -> typing.List[typing.Tuple[str, int, str]]:
        """The section split at its block markers, as (name, offset, body).

        Offsets are kept because a sense still belongs to whichever part-of-speech heading it
        stands under, and that is decided by position in the whole section rather than in the
        block.
        """
        markers = list(dialect.blocks.boundary.finditer(section))
        ends = [match.start() for match in markers[1:]] + [len(section)]
        return [
            (match.group(1).strip(), match.end(), section[match.end():end])
            for match, end in zip(markers, ends)
        ]

    @staticmethod
    def _sense_regions(
        *, section: str, dialect: WikitextDialect,
    ) -> typing.List[typing.Tuple[int, str]]:
        """Where senses may be read from, as (offset, text).

        An edition that files by block states nothing in the line itself, so searching the whole
        section reads its examples, etymology and idioms as senses too.
        """
        if dialect.blocks is None:
            return [(0, section)]
        return [
            (offset, body)
            for name, offset, body in WiktionaryClient._named_blocks(section=section, dialect=dialect)
            if name == dialect.blocks.definitions
        ]

    @staticmethod
    def _sense_is_rare(*, raw: str) -> bool:
        """Whether a sense line's own labels mark it as no longer in use."""
        return any(
            {label.strip().lower() for label in labels.split('|')} & RARITY_LABELS
            for labels in SENSE_LABEL_PATTERN.findall(raw)
        )

    @staticmethod
    def _is_rare(*, argument: str) -> bool:
        """Whether a related word's own qualifier says it is not worth teaching.

        The qualifier is stripped along with every other inline modifier a moment later, so it
        has to be read from the raw argument or not at all. Reading it late is how a synonym the
        dictionary itself marks as dead reached a card with the warning silently removed.
        """
        for match in RARITY_QUALIFIER_PATTERN.finditer(argument):
            if {label.strip().lower() for label in match.group(1).split(',')} & RARITY_LABELS:
                return True
        return False

    @staticmethod
    def _related_words_of(
        *, raw_arguments: str,
    ) -> typing.List[str]:
        """One related-word template's arguments, cleaned, with the ones marked dead left out."""
        arguments = [
            argument.strip() for argument in raw_arguments.split('|')
            if argument.strip() and '=' not in argument
        ]
        return [
            word for word in (
                WiktionaryClient._clean_related_word(argument=argument)
                for argument in arguments if not WiktionaryClient._is_rare(argument=argument)
            ) if word
        ]

    @staticmethod
    def _related_words_by_sense(
        *, section: str, dialect: WikitextDialect, definition_positions: typing.Sequence[int],
    ) -> typing.Tuple[
        typing.List[typing.Tuple[str, ...]], typing.List[typing.Tuple[str, ...]],
    ]:
        """Each sense's own related words, matched to the sense by where they stand in the section.

        A related-word template sits on the line below the sense that owns it, so its owner is the
        last sense beginning at or before it — the same reading by position that decides which
        part-of-speech section a sense belongs to.

        Collecting the templates across the whole section instead is what gave every card built
        from one headword every synonym in the entry, so a card teaching one sense offered the
        synonyms of another. Positions arrive in reading order, which is what lets the owner be
        found by bisection.

        A template standing before the first sense belongs to none, and is dropped rather than
        charged to a sense that does not own it.
        """
        synonyms: typing.List[typing.List[str]] = [[] for _ in definition_positions]
        antonyms: typing.List[typing.List[str]] = [[] for _ in definition_positions]
        for match in dialect.related_word.finditer(section):
            owner = bisect.bisect_right(definition_positions, match.start()) - 1
            if owner < 0:
                continue
            kind, raw_arguments = match.groups()
            target = synonyms if kind in dialect.synonym_template_names else antonyms
            target[owner].extend(WiktionaryClient._related_words_of(raw_arguments=raw_arguments))
        return (
            [tuple(words) for words in synonyms],
            [tuple(words) for words in antonyms],
        )

    @staticmethod
    def _related_words(
        *, section: str, dialect: WikitextDialect,
    ) -> typing.Tuple[typing.List[str], typing.List[str]]:
        """The entry-wide synonym and antonym lists, read across every sense in the section.

        Kept alongside `_related_words_by_sense` for an edition whose senses this stage cannot
        tell apart from the related words they carry (`WiktionaryResult.related_words_are_per_sense`
        false); such an edition has nothing finer than the entry to attach a word to.
        """
        synonyms: typing.List[str] = []
        antonyms: typing.List[str] = []
        if dialect.blocks is None:
            for match in dialect.related_word.finditer(section):
                kind, raw_arguments = match.groups()
                target = synonyms if kind in dialect.synonym_template_names else antonyms
                target.extend(WiktionaryClient._related_words_of(raw_arguments=raw_arguments))
            return synonyms, antonyms

        for name, _, body in WiktionaryClient._named_blocks(section=section, dialect=dialect):
            if name == dialect.blocks.synonyms:
                target = synonyms
            elif name == dialect.blocks.antonyms:
                target = antonyms
            else:
                continue
            body = ITALIC_SPAN_PATTERN.sub('', REFERENCE_TAG_PATTERN.sub('', body))
            for match in WIKILINK_PATTERN.finditer(body):
                word = WiktionaryClient._clean_related_word(argument=match.group(2) or match.group(1))
                if word:
                    target.append(word)
        return synonyms, antonyms

    @staticmethod
    def senses(*, wikitext: str, language: language_config.LanguageConfig) -> typing.List[WiktionaryDefinition]:
        """A lemma's other-meanings candidates: every sense the page states, cleaned up for reuse.

        `_parse` keeps the raw definition list as Wiktionary wrote it, markup and all, because
        that is what the enrichment stage needs to pick one sense from. Showing a learner the
        rest of a headword's senses needs the same list with the wikitext debris filtered out and
        the near-duplicates collapsed, which is what this returns.
        """
        parsed = WiktionaryClient._parse(wikitext=wikitext, language=language)
        seen: typing.Set[str] = set()
        kept: typing.List[WiktionaryDefinition] = []
        for definition in parsed.definitions:
            text = definition.text.strip()
            if (
                SENSE_MARKER_PREFIX_PATTERN.match(text)
                or SENSE_TEMPLATE_START_PATTERN.match(text)
                or SENSE_LEAD_IN_PATTERN.match(text)
                or len(text) < SENSE_MINIMUM_LENGTH
                or '{{' in text
            ):
                continue
            if definition.is_rare or text.lower() in seen:
                continue
            seen.add(text.lower())
            kept.append(WiktionaryDefinition(text=text, part_of_speech=definition.part_of_speech))
        return kept[:SENSE_LIMIT]

    @staticmethod
    def _clean_related_word(*, argument: str) -> str:
        """One entry of a synonym list, stripped of the link and annotation syntax around the word.

        A related-word template carries more than words: a pointer to the thesaurus page for the
        whole group, a link aimed at one section of an entry, and inline modifiers stating register
        or which sense is meant. Passed through, each becomes a card that teaches its own markup —
        and worse, gets glossed as though it were a word.

        Arguments are separated by the same pipe a wiki link uses inside itself, so an argument can
        also arrive cut through the middle of a link or a modifier. What is left of either half is
        markup with no word in it, and goes the same way.
        """
        if THESAURUS_LINK_MARKER in argument:
            return ''
        word = INLINE_MODIFIER_PATTERN.sub('', argument)
        word = WIKI_MARKUP_PATTERN.sub('', word)
        word = word.split(SECTION_LINK_MARKER, 1)[0].strip()
        if ANGLE_BRACKET_PATTERN.search(word):
            return ''
        if word.lower().startswith(LIST_PROSE_PREFIX) or not LETTER_PATTERN.search(word):
            return ''
        return word

    @staticmethod
    def _find_form_redirect(*, wikitext: str, language: language_config.LanguageConfig) -> typing.Optional[str]:
        dialect = dialect_for(host=language.wiktionary_host, language=language)
        section = WiktionaryClient._extract_language_section(wikitext=wikitext, language=language)
        match = dialect.form_pointer.search(section)
        if match is None:
            return None
        args = [arg.strip() for arg in match.group(2).split('|') if arg.strip() and '=' not in arg]
        # Editions that tag the template with a language code put it first; the base form follows.
        if args and args[0] == language.code:
            args = args[1:]
        return args[0] if args else None

    @staticmethod
    def _extract_language_section(*, wikitext: str, language: language_config.LanguageConfig) -> str:
        dialect = dialect_for(host=language.wiktionary_host, language=language)
        start_match = dialect.language_section.search(wikitext)
        if start_match is None:
            return ''
        end_match = SECTION_HEADING_PATTERN.search(wikitext, start_match.end())
        end = end_match.start() if end_match else len(wikitext)
        return wikitext[start_match.end():end]

    @staticmethod
    def _render_template(match: 're.Match', *, dialect: WikitextDialect) -> str:
        name = match.group(1).strip().lower()
        if name not in dialect.templates_rendering_argument:
            return ''
        arguments = [part for part in match.group(2).split('|') if part and '=' not in part]
        return arguments[-1].strip() if arguments else ''

    @staticmethod
    def _clean_text(*, text: str, dialect: WikitextDialect) -> str:
        text = REFERENCE_TAG_PATTERN.sub('', text)
        text = WIKILINK_PATTERN.sub(lambda match: match.group(2) or match.group(1), text)
        text = WiktionaryClient._strip_templates(text=text, dialect=dialect)
        text = EMPHASIS_PATTERN.sub('', text)
        text = WHITESPACE_PATTERN.sub(' ', text).strip()
        return text.rstrip('.').strip()

    @staticmethod
    def _strip_templates(*, text: str, dialect: WikitextDialect) -> str:
        """Take the templates out, innermost first, until none is left.

        A template pattern cannot match one that holds another, so a single pass over an edition's
        quotation markup leaves the outer wrapper behind — and a sense that still carries markup is
        thrown away later rather than shown, so the sense disappears instead of the markup.
        """
        for _ in range(TEMPLATE_NESTING_LIMIT):
            stripped = TEMPLATE_PATTERN.sub(
                lambda match: WiktionaryClient._render_template(match, dialect=dialect), text,
            )
            if stripped == text:
                return stripped
            text = stripped
        return text


class TatoebaClient:
    """Fetches example sentences and picks the ones suitable for A1-A2 material."""

    @staticmethod
    def fetch_one(
        *, lemma: str, cache: Cache, language: language_config.LanguageConfig,
    ) -> typing.Tuple[FetchStatus, typing.List[TatoebaExample]]:
        cached = cache.load(source=Source.TATOEBA, lemma=lemma)
        if cached is not None:
            if not cached.get('found'):
                return FetchStatus.NOT_FOUND, []
            return FetchStatus.FOUND, TatoebaClient._select(
                lemma=lemma, results=cached.get('results', []), language=language,
            )

        result = HttpClient.get(url=TatoebaClient._build_url(lemma=lemma, language=language))
        if result.status == FetchStatus.FAILED:
            return FetchStatus.FAILED, []
        if result.status == FetchStatus.NOT_FOUND:
            cache.store(source=Source.TATOEBA, lemma=lemma, payload={'found': False})
            return FetchStatus.NOT_FOUND, []

        body = json.loads(result.payload)
        results = body.get('results') or []
        cache.store(source=Source.TATOEBA, lemma=lemma, payload={'found': True, 'results': results})
        return FetchStatus.FOUND, TatoebaClient._select(lemma=lemma, results=results, language=language)

    @staticmethod
    def _build_url(*, lemma: str, language: language_config.LanguageConfig) -> str:
        return TATOEBA_API_URL_TEMPLATE.format(
            corpus_language_code=language.corpus_language_code,
            lemma=urllib.parse.quote(lemma),
            limit=TATOEBA_SEARCH_LIMIT,
        )

    @staticmethod
    def _select(
        *, lemma: str, results: typing.List[dict], language: language_config.LanguageConfig,
    ) -> typing.List[TatoebaExample]:
        candidates = []
        for result in results:
            text = (result.get('text') or '').strip()
            if not TatoebaClient._is_relevant(lemma=lemma, text=text):
                continue
            word_count = len(text.split())
            if not (TATOEBA_MIN_WORDS <= word_count <= TATOEBA_MAX_WORDS):
                continue
            native_text, pivot_text = TatoebaClient._pick_translations(result=result, language=language)
            priority = 0 if native_text else (1 if pivot_text else 2)
            candidates.append((priority, word_count, TatoebaExample(target=text, native=native_text, pivot=pivot_text)))
        candidates.sort(key=lambda candidate: (candidate[0], candidate[1]))
        return [candidate[2] for candidate in candidates[:TATOEBA_MAX_EXAMPLES]]

    @staticmethod
    def _pick_translations(
        *, result: dict, language: language_config.LanguageConfig,
    ) -> typing.Tuple[str, str]:
        """Keep only the translations the card will show, matched on the corpus's own language code.

        The corpus returns whatever its contributors wrote, so a wrong match here does not leave a
        gap — it writes a neighbouring language into the column the card labels as the reader's.
        """
        native_code = language.native_corpus_code
        pivot_code = language.pivot_corpus_code
        native_text = ''
        pivot_text = ''
        for translation_group in result.get('translations') or []:
            for translation in translation_group:
                code = translation.get('lang')
                if code == native_code and not native_text:
                    native_text = (translation.get('text') or '').strip()
                elif pivot_code is not None and code == pivot_code and not pivot_text:
                    pivot_text = (translation.get('text') or '').strip()
        return native_text, pivot_text

    @staticmethod
    def _is_relevant(*, lemma: str, text: str) -> bool:
        """Tatoeba's search is stemmed and fuzzy; keep only sentences containing a plausibly related word."""
        folded_lemma = TatoebaClient._fold(text=TatoebaClient._relevance_lemma(lemma=lemma))
        for index, match in enumerate(WORD_TOKEN_PATTERN.finditer(text)):
            token = match.group(0)
            # A capitalized word past the sentence's first position reads as a proper noun (e.g.
            # the surname "Castro" against the verb "castrar"), not an inflected form; a prefix
            # match loose enough to admit real conjugations can't otherwise tell the two apart.
            if index > 0 and token[:1].isupper():
                continue
            if TatoebaClient._word_matches_lemma(word=TatoebaClient._fold(text=token), lemma=folded_lemma):
                return True
        return False

    @staticmethod
    def _relevance_lemma(*, lemma: str) -> str:
        words = lemma.split()
        return max(words, key=len) if words else lemma

    @staticmethod
    def _fold(*, text: str) -> str:
        normalized = unicodedata.normalize('NFKD', text)
        return ''.join(character for character in normalized if not unicodedata.combining(character)).lower()

    @staticmethod
    def _word_matches_lemma(*, word: str, lemma: str) -> bool:
        if word == lemma:
            return True
        if len(lemma) < TATOEBA_RELEVANCE_MIN_LEMMA_LENGTH:
            return False
        if len(word) > len(lemma) + TATOEBA_RELEVANCE_MAX_LENGTH_SURPLUS:
            return False
        required_prefix_length = len(lemma) - TATOEBA_RELEVANCE_PREFIX_DROP_ALLOWANCE
        return TatoebaClient._common_prefix_length(first=word, second=lemma) >= required_prefix_length

    @staticmethod
    def _common_prefix_length(*, first: str, second: str) -> int:
        length = 0
        for left, right in zip(first, second):
            if left != right:
                break
            length += 1
        return length


class RowEnricher:
    """Merges RAE, Wiktionary and Tatoeba lookups into one output row."""

    @staticmethod
    def lookup_lemma(*, word: str) -> str:
        # Never look up by the sense-disambiguation key; the bare headword lives in `word`, and
        # only the text before its first comma is the actual dictionary form.
        return word.split(',', 1)[0].strip()

    @staticmethod
    def enrich(
        *,
        row: typing.Dict[str, str],
        language: language_config.LanguageConfig,
        rae_senses: typing.List[RaeSense],
        wiktionary: WiktionaryResult,
        tatoeba_examples: typing.List[TatoebaExample],
        bands: frequency.FrequencyBands,
    ) -> typing.Dict[str, str]:
        enriched = dict(row)
        lemma = RowEnricher.lookup_lemma(word=row['word'])
        pos_tokens = row['part_of_speech'].split()
        chosen_senses = RowEnricher._select_senses(senses=rae_senses, pos_tokens=pos_tokens)

        # The sense is chosen once and both halves of the card are built from it. Choosing it for
        # the definition while taking the related words from the whole entry is what let a card
        # teach one meaning and offer another meaning's synonyms.
        chosen_definition = (
            RowEnricher._select_definition(
                definitions=wiktionary.definitions,
                pos_tokens=pos_tokens,
                chosen_text=row.get(language.definition_column, ''),
            )
            if wiktionary.definitions else None
        )
        definition, definition_source = RowEnricher._merge_definition(
            chosen_senses=chosen_senses, chosen_definition=chosen_definition,
        )
        wiktionary_synonyms, wiktionary_antonyms = RowEnricher._wiktionary_related(
            wiktionary=wiktionary, chosen_definition=chosen_definition,
        )
        synonyms = RowEnricher.merge_related(
            lemma=lemma,
            rae_words=RowEnricher._select_related_words(
                related=[word for sense in chosen_senses for word in sense.synonyms],
            ),
            wiktionary_words=wiktionary_synonyms,
            bands=bands,
        )
        antonyms = RowEnricher.merge_related(
            lemma=lemma,
            rae_words=RowEnricher._select_related_words(
                related=[word for sense in chosen_senses for word in sense.antonyms],
            ),
            wiktionary_words=wiktionary_antonyms,
            bands=bands,
        )

        enriched[language.definition_column] = definition
        enriched['synonyms'] = synonyms
        enriched['antonyms'] = antonyms
        # The model stage fills this by moving the dictionary wording here once it simplifies
        # the definition; the dictionaries stage never writes to it.
        enriched[language.dictionary_definition_column] = ''
        enriched[DEFINITION_SOURCE_COLUMN] = definition_source
        enriched[DICTIONARY_EXAMPLES_COLUMN] = RowEnricher._merge_dictionary_examples(chosen_senses=chosen_senses)
        RowEnricher._apply_examples(enriched=enriched, tatoeba_examples=tatoeba_examples, language=language)

        return enriched

    @staticmethod
    def _apply_examples(
        *, enriched: typing.Dict[str, str], tatoeba_examples: typing.List[TatoebaExample], language: language_config.LanguageConfig,
    ) -> None:
        target_column = language.examples_column(suffix=language.target)
        native_column = language.examples_column(suffix=language.native)
        if enriched[target_column]:
            enriched[EXAMPLES_SOURCE_COLUMN] = ExamplesSource.INPUT
        elif tatoeba_examples:
            enriched[target_column] = '<br>'.join(example.target for example in tatoeba_examples)
            enriched[native_column] = '<br>'.join(example.native for example in tatoeba_examples)
            if language.pivot is not None:
                pivot_column = language.examples_column(suffix=language.pivot)
                enriched[pivot_column] = '<br>'.join(example.pivot for example in tatoeba_examples)
            enriched[EXAMPLES_SOURCE_COLUMN] = ExamplesSource.TATOEBA
        else:
            enriched[EXAMPLES_SOURCE_COLUMN] = ''

    @staticmethod
    def _select_senses(*, senses: typing.List[RaeSense], pos_tokens: typing.List[str]) -> typing.List[RaeSense]:
        if not pos_tokens:
            return senses
        filtered = [sense for sense in senses if RowEnricher._category_matches(category=sense.category, pos_tokens=pos_tokens)]
        return filtered if filtered else senses

    @staticmethod
    def _category_matches(*, category: str, pos_tokens: typing.List[str]) -> bool:
        if not category:
            return False
        category_lower = category.lower()
        return any(token.lower() in category_lower or category_lower in token.lower() for token in pos_tokens)

    @staticmethod
    def _merge_definition(
        *,
        chosen_senses: typing.List[RaeSense],
        chosen_definition: typing.Optional[WiktionaryDefinition],
    ) -> typing.Tuple[str, str]:
        if chosen_senses and chosen_senses[0].description:
            return chosen_senses[0].description, DefinitionSource.RAE
        if chosen_definition is not None:
            return chosen_definition.text, DefinitionSource.WIKTIONARY
        return '', ''

    @staticmethod
    def _wiktionary_related(
        *, wiktionary: WiktionaryResult, chosen_definition: typing.Optional[WiktionaryDefinition],
    ) -> typing.Tuple[typing.List[str], typing.List[str]]:
        """The related words of the sense the card teaches, where the edition says which sense owns them.

        Where it does, the chosen sense's own lists are all there is: falling back to the entry-wide
        list for a sense that states none puts another meaning's synonyms on the card, which is the
        whole failure this replaces. A sense with none simply has none, and the model stage already
        fills only what the dictionaries left empty.

        Where the edition files related words under a block instead, it never said which sense owns
        them, and the entry-wide lists are the only answer available.
        """
        if not wiktionary.related_words_are_per_sense:
            return wiktionary.synonyms, wiktionary.antonyms
        if chosen_definition is None:
            return [], []
        return list(chosen_definition.synonyms), list(chosen_definition.antonyms)

    @staticmethod
    def _select_definition(
        *, definitions: typing.List[WiktionaryDefinition], pos_tokens: typing.List[str], chosen_text: str = '',
    ) -> WiktionaryDefinition:
        """The first sense stated for the part of speech this row is, or the first sense at all.

        Falling back matters as much as matching: an edition whose headings this stage cannot read
        still has to yield a definition, and the entry's opening sense is the best guess going.
        """
        # A sense already chosen for this word wins; the entry's own order is only a fallback.
        chosen = next((definition for definition in definitions if chosen_text and definition.text == chosen_text), None)
        if chosen is not None:
            return chosen
        # A dead sense is taught only when the entry has nothing else to offer.
        matching = [
            definition for definition in definitions
            if not pos_tokens
            or RowEnricher._category_matches(category=definition.part_of_speech, pos_tokens=pos_tokens)
        ] or definitions
        return next((definition for definition in matching if not definition.is_rare), matching[0])

    @staticmethod
    def _select_related_words(*, related: typing.List[RaeRelatedWord]) -> typing.List[str]:
        """Prefer register-neutral RAE words; a colloquial synonym beats no synonym at all."""
        neutral = [item.word for item in related if not item.usage]
        return neutral if neutral else [item.word for item in related]

    @staticmethod
    def _strip_homograph_marker(*, word: str) -> str:
        """RAE and Wiktionary suffix a homograph number (e.g. "jeta1") to some words; drop it."""
        match = TRAILING_DIGIT_SUFFIX_PATTERN.match(word)
        return match.group(1) if match else word

    @staticmethod
    def merge_related(
        *,
        lemma: str,
        rae_words: typing.List[str],
        wiktionary_words: typing.List[str],
        bands: frequency.FrequencyBands,
    ) -> str:
        """The related words worth showing, in the order the dictionaries stated them.

        A synonym rarer than the word it explains leaves the learner holding two unknown words
        instead of one, so anything far below the headword's own frequency is dropped. The order is
        left as the dictionaries wrote it, which tracks usefulness better than sorting by frequency
        does — sorted, `intend` leads with `think`, which is broader than the word it explains.
        """
        lemma_lower = lemma.lower()
        floor = bands.zipf_for(word=lemma) - RELATED_WORD_RARITY_MARGIN
        seen = set()
        merged = []
        for raw_word in rae_words + wiktionary_words:
            word = RowEnricher._strip_homograph_marker(word=raw_word.strip())
            word_lower = word.lower()
            if not word or word_lower == lemma_lower or word_lower in seen:
                continue
            seen.add(word_lower)
            if bands.zipf_for(word=word) < floor:
                continue
            merged.append(word)
            if len(merged) >= RELATED_WORDS_LIMIT:
                break
        return ', '.join(merged)

    @staticmethod
    def _merge_dictionary_examples(*, chosen_senses: typing.List[RaeSense]) -> str:
        seen = set()
        examples = []
        for sense in chosen_senses:
            for example in sense.examples:
                if len(examples) >= RAE_MAX_EXAMPLES:
                    break
                if example not in seen:
                    seen.add(example)
                    examples.append(example)
            if len(examples) >= RAE_MAX_EXAMPLES:
                break
        return '<br>'.join(examples)


class Stats:
    """Coverage counters reported in the final summary."""

    def __init__(self) -> None:
        self.total_rows = 0
        self.definitions_rae = 0
        self.definitions_wiktionary = 0
        self.synonyms = 0
        self.antonyms = 0
        self.examples_input = 0
        self.examples_tatoeba = 0
        self.failed_requests = 0

    def record_row(self, *, row: typing.Dict[str, str]) -> None:
        self.total_rows += 1
        if row[DEFINITION_SOURCE_COLUMN] == DefinitionSource.RAE:
            self.definitions_rae += 1
        elif row[DEFINITION_SOURCE_COLUMN] == DefinitionSource.WIKTIONARY:
            self.definitions_wiktionary += 1
        if row['synonyms']:
            self.synonyms += 1
        if row['antonyms']:
            self.antonyms += 1
        if row[EXAMPLES_SOURCE_COLUMN] == ExamplesSource.INPUT:
            self.examples_input += 1
        elif row[EXAMPLES_SOURCE_COLUMN] == ExamplesSource.TATOEBA:
            self.examples_tatoeba += 1

    def summary(self) -> str:
        definitions_total = self.definitions_rae + self.definitions_wiktionary
        examples_total = self.examples_input + self.examples_tatoeba
        return (
            f"rows: {self.total_rows}\n"
            f"definitions: {definitions_total} (rae={self.definitions_rae}, wiktionary={self.definitions_wiktionary})\n"
            f"synonyms: {self.synonyms}\n"
            f"antonyms: {self.antonyms}\n"
            f"examples: {examples_total} (input={self.examples_input}, tatoeba={self.examples_tatoeba})\n"
            f"failed requests: {self.failed_requests}"
        )


class TsvIo:
    """Minimal tab-separated reader/writer matching the source list export's own format."""

    @staticmethod
    def read(
        *, path: pathlib.Path, limit: typing.Optional[int],
    ) -> typing.Tuple[typing.List[str], typing.List[typing.Dict[str, str]]]:
        lines = path.read_text(encoding='utf-8').splitlines()
        if not lines:
            return [], []
        header = lines[0].split('\t')
        data_lines = lines[1:] if limit is None else lines[1:1 + limit]
        rows = []
        for line in data_lines:
            values = line.split('\t')
            values += [''] * (len(header) - len(values))
            rows.append(dict(zip(header, values)))
        return header, rows

    @staticmethod
    def write(*, path: pathlib.Path, header: typing.List[str], rows: typing.List[typing.Dict[str, str]]) -> None:
        with path.open('w', encoding='utf-8') as stream:
            stream.write('\t'.join(header) + '\n')
            for row in rows:
                stream.write('\t'.join(row.get(column, '') for column in header) + '\n')


class Pipeline:
    """Runs per-source lookups over a thread pool, deduplicating by lemma."""

    @staticmethod
    def fetch_many(
        *,
        lemmas: typing.List[str],
        max_workers: int,
        fetch_one: typing.Callable[[str], typing.Tuple[FetchStatus, LookupResultT]],
        source: Source,
        stats: Stats,
    ) -> typing.Dict[str, LookupResultT]:
        unique_lemmas = list(dict.fromkeys(lemmas))
        results: typing.Dict[str, LookupResultT] = {}
        total = len(unique_lemmas)
        if total == 0:
            return results

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_lemma = {executor.submit(fetch_one, lemma): lemma for lemma in unique_lemmas}
            completed = 0
            for future in concurrent.futures.as_completed(future_to_lemma):
                lemma = future_to_lemma[future]
                status, parsed = future.result()
                if status == FetchStatus.FAILED:
                    stats.failed_requests += 1
                results[lemma] = parsed
                completed += 1
                print(f"[{source}] {completed}/{total} {lemma}", file=sys.stderr)
        return results


def main_for(
    *,
    language: language_config.LanguageConfig,
    root: pathlib.Path,
    limit: typing.Optional[int] = None,
    refresh: bool = False,
    input_path: typing.Optional[pathlib.Path] = None,
    output_path: typing.Optional[pathlib.Path] = None,
) -> None:
    # Wiktionary works for every language and has no daily cap; a specialised dictionary is
    # opt-in per language because those tend to be rate-capped far below a deck's size.
    use_rae = language.dictionary == RAE_DICTIONARY_NAME

    data_directory = root / 'data' / language.target
    input_path = input_path or data_directory / DEFAULT_INPUT_FILENAME
    output_path = output_path or data_directory / DEFAULT_OUTPUT_FILENAME

    header, rows = TsvIo.read(path=input_path, limit=limit)
    output_header = header + [
        language.dictionary_definition_column, DICTIONARY_EXAMPLES_COLUMN, DEFINITION_SOURCE_COLUMN, EXAMPLES_SOURCE_COLUMN,
    ]

    cache_root = data_directory / CACHE_DIRECTORY_NAME
    cache = Cache(root=cache_root, refresh=refresh, edition=language.wiktionary_host)
    stats = Stats()

    lemma_by_row = [RowEnricher.lookup_lemma(word=row['word']) for row in rows]

    rae_by_lemma = Pipeline.fetch_many(
        lemmas=lemma_by_row,
        max_workers=RAE_MAX_WORKERS,
        fetch_one=lambda lemma: RaeClient.fetch_one(lemma=lemma, cache=cache),
        source=Source.RAE,
        stats=stats,
    ) if use_rae else {}
    wiktionary_by_lemma = WiktionaryClient.fetch_many(
        lemmas=lemma_by_row, cache=cache, language=language, stats=stats,
    )
    target_examples_column = language.examples_column(suffix=language.target)
    tatoeba_lemmas = [lemma for lemma, row in zip(lemma_by_row, rows) if not row[target_examples_column]]
    tatoeba_by_lemma = Pipeline.fetch_many(
        lemmas=tatoeba_lemmas,
        max_workers=TATOEBA_MAX_WORKERS,
        fetch_one=lambda lemma: TatoebaClient.fetch_one(lemma=lemma, cache=cache, language=language),
        source=Source.TATOEBA,
        stats=stats,
    )

    empty_wiktionary_result = WiktionaryResult(definitions=[], synonyms=[], antonyms=[])
    # Built once: the lookup loads a corpus list, and every row asks it the same kind of question.
    bands = frequency.FrequencyBands(language_code=language.target)
    enriched_rows = []
    for lemma, row in zip(lemma_by_row, rows):
        enriched = RowEnricher.enrich(
            row=row,
            language=language,
            rae_senses=rae_by_lemma.get(lemma, []),
            wiktionary=wiktionary_by_lemma.get(lemma, empty_wiktionary_result),
            tatoeba_examples=tatoeba_by_lemma.get(lemma, []),
            bands=bands,
        )
        stats.record_row(row=enriched)
        enriched_rows.append(enriched)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    TsvIo.write(path=output_path, header=output_header, rows=enriched_rows)

    print(stats.summary())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        'input', nargs='?', type=pathlib.Path, default=None,
        help="Input TSV; defaults to data/<target>/words.tsv",
    )
    parser.add_argument(
        'output', nargs='?', type=pathlib.Path, default=None,
        help="Path of the enriched TSV file to write; defaults to data/<target>/enriched.tsv",
    )
    parser.add_argument('--limit', type=int, default=None, help="Process only the first N rows")
    parser.add_argument('--refresh', action='store_true', help="Ignore the disk cache and refetch everything")
    language_config.add_language_arguments(parser)
    arguments = parser.parse_args()

    root = language_config.PROJECT_ROOT
    language = language_config.language_from(arguments, root=root)
    main_for(
        language=language,
        root=root,
        limit=arguments.limit,
        refresh=arguments.refresh,
        input_path=arguments.input,
        output_path=arguments.output,
    )


if __name__ == '__main__':
    main()
