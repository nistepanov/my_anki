"""Put a verb's actual forms on its card, not just the one form a sentence rarely uses.

A card that teaches `tener` and stops there teaches the word a learner will never say: what
comes out of the mouth is `tengo`, `tuve`, `tendría`. The forms are published as conjugation
tables, and the useful part of one is small — the present tense, and a single form of each
other tense to anchor the stem.

The tables come from the English Wiktionary because it renders them for every language it
covers and labels their rows in English regardless of the language being conjugated, so one
parser serves every deck. Which rows are worth keeping is per-language and comes from the
language config.

Only the rendered page carries the forms — the source wikitext holds a template call and
nothing else — so this reads HTML, which is why it keeps to the two structural facts the
table has always had: forms sit in their own elements, and the row's first cell names it.
"""

import argparse
import concurrent.futures
import html
import json
import pathlib
import re
import sys
import typing
import urllib.parse

from . import language_config
from . import media

WIKTIONARY_HOST = 'en.wiktionary.org'
PARSE_URL_TEMPLATE = (
    f'https://{WIKTIONARY_HOST}/w/api.php?action=parse&page={{page}}&prop=text'
    '&format=json&formatversion=2'
)
CACHE_PROVIDER = 'inflections'
CARDS_FILENAME = 'cards.tsv'
INFLECTION_COLUMN = 'inflection'

# A page covers every language that shares the spelling; sections start at heading level two.
SECTION_HEADING_PATTERN = re.compile(r'<div class="mw-heading mw-heading2">')
TABLE_PATTERN = re.compile(r'<table class="[^"]*inflection-table.*?</table>', re.S)
ROW_PATTERN = re.compile(r'<tr>(.*?)</tr>', re.S)
CELL_PATTERN = re.compile(r'<t[hd][^>]*>(.*?)</t[hd]>', re.S)
# Each form is wrapped as a form-of link; a cell holding several offers regional variants.
FORM_PATTERN = re.compile(r'<span[^>]*class="[^"]*form-of[^"]*"[^>]*>(.*?)</span>', re.S)
# Superscripts name the pronoun a regional variant belongs to, which the card does not show.
SUPERSCRIPT_PATTERN = re.compile(r'<sup>.*?</sup>', re.S)
TAG_PATTERN = re.compile(r'<[^>]+>')
MISSING_FORM_MARKERS = ('—', '-', '')


class ConjugationTable:
    """The rows of one rendered conjugation table, addressed by their English row label."""

    def __init__(self, *, rows: typing.Dict[str, typing.List[str]]):
        self._rows = rows

    def __bool__(self) -> bool:
        return bool(self._rows)

    def select(self, *, wanted: typing.Sequence[str], first_only: typing.Sequence[str]) -> dict:
        """Keep the handful of rows a card has room for, in the order the config names them."""
        selected = {}
        for label in wanted:
            forms = self._rows.get(label)
            if not forms:
                continue
            selected[label] = forms[:1] if label in first_only else forms
        return selected

    @classmethod
    def parse(cls, *, markup: str, section_anchor: str) -> 'ConjugationTable':
        section = cls._section(markup=markup, anchor=section_anchor)
        table = TABLE_PATTERN.search(section) if section else None
        if table is None:
            return cls(rows={})
        rows: typing.Dict[str, typing.List[str]] = {}
        group = ''
        for row_markup in ROW_PATTERN.findall(table.group(0)):
            cells = CELL_PATTERN.findall(row_markup)
            if len(cells) < 2:
                continue
            label = cls._plain(markup=cells[0]).lower()
            forms = [cls._form(markup=cell) for cell in cells[1:]]
            forms = [form for form in forms if form not in MISSING_FORM_MARKERS]
            # A row carrying no form markup is naming the rows under it, not conjugating.
            if not any(FORM_PATTERN.search(cell) for cell in cells[1:]):
                group = label
                continue
            if not label or not forms:
                continue
            rows.setdefault(label, forms)
            # Under a group header the row's own label is a bare 'singular', which means
            # nothing on its own; the two together are what the config can ask for.
            if group:
                rows.setdefault(f'{group} {label}', forms)
        return cls(rows=rows)

    @staticmethod
    def _section(*, markup: str, anchor: str) -> str:
        start = markup.find(f'id="{anchor}"')
        if start < 0:
            return ''
        following = SECTION_HEADING_PATTERN.search(markup, start)
        return markup[start:following.start() if following else len(markup)]

    @classmethod
    def _form(cls, *, markup: str) -> str:
        """The first form in the cell; the rest are regional variants of the same slot."""
        match = FORM_PATTERN.search(SUPERSCRIPT_PATTERN.sub('', markup))
        return cls._plain(markup=match.group(1)) if match is not None else ''

    @staticmethod
    def _plain(*, markup: str) -> str:
        return html.unescape(TAG_PATTERN.sub('', markup)).strip().replace('\n', ' ')


class InflectionFetcher:
    """Looks a lemma's table up once and remembers the answer, found or not."""

    def __init__(self, *, language: language_config.LanguageConfig, data_directory: pathlib.Path):
        settings = language.inflection
        self._anchor = settings.get('section', language.target_name)
        self._wanted = settings.get('rows', ())
        self._first_only = settings.get('single_form_rows', ())
        self._cache = media.Cache(root=data_directory / media.CACHE_DIRECTORY_NAME)
        self._stats = media.Stats()
        self._throttles = media.HostThrottleRegistry(
            max_workers_per_host=media.MAX_WORKERS_PER_HOST,
            min_interval_seconds=media.HOST_MIN_REQUEST_INTERVAL_SECONDS,
        )

    @property
    def is_configured(self) -> bool:
        return bool(self._wanted)

    @property
    def stats(self) -> media.Stats:
        return self._stats

    def forms_for(self, *, lemma: str) -> dict:
        cached = self._cache.load(provider=CACHE_PROVIDER, identifier=lemma)
        if cached is not None:
            return cached.get('forms', {})
        # A multi-word entry inflects on its head word and is not itself a dictionary page:
        # `tener miedo de` becomes `tengo miedo de`, so the forms wanted are the head verb's.
        forms = self._lookup(lemma=lemma.split()[0] if lemma.split() else lemma)
        self._cache.store(provider=CACHE_PROVIDER, identifier=lemma, payload={'forms': forms})
        return forms

    def _lookup(self, *, lemma: str) -> dict:
        url = PARSE_URL_TEMPLATE.format(page=urllib.parse.quote(lemma))
        response = media.HttpClient.get_text(
            url=url, throttle=self._throttles.for_host(host=WIKTIONARY_HOST), stats=self._stats,
        )
        if response.status == media.FetchStatus.FAILED or response.text is None:
            return {}
        payload = json.loads(response.text)
        # A page that does not exist is a normal 200 carrying an error, not an HTTP 404.
        markup = payload.get('parse', {}).get('text', '') if 'error' not in payload else ''
        table = ConjugationTable.parse(markup=markup, section_anchor=self._anchor)
        return table.select(wanted=self._wanted, first_only=self._first_only)


def main_for(
    *,
    language: language_config.LanguageConfig,
    root: pathlib.Path,
    limit: typing.Optional[int] = None,
    input_path: typing.Optional[pathlib.Path] = None,
) -> None:
    data_directory = language.data_directory(root=root)
    cards_path = input_path if input_path is not None else data_directory / CARDS_FILENAME
    rows = language_config.TsvFile.read(cards_path)

    fetcher = InflectionFetcher(language=language, data_directory=data_directory)
    if not fetcher.is_configured:
        print("no inflection rows configured for this language — nothing to do", file=sys.stderr)
        return

    inflected = language.inflection.get('part_of_speech', 'verb')
    pending = [
        row for row in rows
        if inflected in row.get('part_of_speech', '').split() and not row.get(INFLECTION_COLUMN)
    ]
    if limit is not None:
        pending = pending[:limit]
    if not pending:
        print("every inflected word already has its forms")
        return

    lemmas = list(dict.fromkeys(row['word'] for row in pending))
    forms_by_lemma: typing.Dict[str, dict] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=media.EXECUTOR_MAX_WORKERS) as pool:
        futures = {pool.submit(fetcher.forms_for, lemma=lemma): lemma for lemma in lemmas}
        for done, future in enumerate(concurrent.futures.as_completed(futures), start=1):
            forms_by_lemma[futures[future]] = future.result()
            if done % media.MANIFEST_FLUSH_EVERY == 0:
                print(f"  {done}/{len(lemmas)} lemmas", file=sys.stderr)

    filled = 0
    for row in rows:
        forms = forms_by_lemma.get(row['word'])
        if forms and not row.get(INFLECTION_COLUMN):
            row[INFLECTION_COLUMN] = json.dumps(forms, ensure_ascii=False)
            filled += 1
    language_config.TsvFile.write(cards_path, rows=rows, columns=language.card_columns)
    print(f"{filled} of {len(pending)} words got their forms; {len(lemmas)} lemmas looked up")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', nargs='?', type=pathlib.Path, default=None)
    parser.add_argument('--limit', type=int, default=None, help="Process only the first N words")
    language_config.add_language_arguments(parser)
    arguments = parser.parse_args()

    root = language_config.PROJECT_ROOT
    main_for(
        language=language_config.language_from(arguments, root=root),
        root=root,
        limit=arguments.limit,
        input_path=arguments.input,
    )


if __name__ == '__main__':
    main()
