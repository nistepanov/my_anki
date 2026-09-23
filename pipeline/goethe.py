"""Import the official Goethe-Institut word lists, which are the German deck's word list.

Every other language here takes its level from a model judgement anchored on frequency,
because no publisher states one. German is the exception: the Goethe-Institut publishes one
word list per exam level, and each list is cumulative, so a word's level is simply the first
list that holds it. That is a fact rather than an estimate, and it decides the deck's subdecks.

The lists carry more than headwords. A noun is printed with its article and its plural, a verb
with its principal parts and the auxiliary its perfect takes, and every entry with example
sentences written for the level. All of that is exactly what a German card needs and what no
later stage could recover cheaply, so it is read here rather than thrown away.

The lists are PDFs laid out in columns, so the text is extracted with its coordinates and the
columns are separated by them. A word is classified by its own x position rather than by the
block it landed in, because the extractor sometimes merges a headword and its example into one
block and a block-level rule then reads the pair as a single headword.
"""

import argparse
import json
import pathlib
import re
import subprocess
import sys
import typing
import urllib.request
import xml.etree.ElementTree as ElementTree

from . import language_config
from . import reword

CACHE_DIRECTORY = pathlib.Path('cache') / 'goethe'
PDFTOTEXT = 'pdftotext'
# goethe.de refuses a request without one.
USER_AGENT = 'my-anki/1.0 (personal deck builder)'
XHTML_NAMESPACE = '{http://www.w3.org/1999/xhtml}'

ARTICLE_GENDERS = {'der': 'm', 'die': 'f', 'das': 'n'}
# A perfect is printed with the auxiliary it takes, and which auxiliary that is has to be
# memorised, so it stays part of the form rather than being stripped as noise.
AUXILIARIES = ('hat', 'ist', 'haben', 'sind')
UMLAUT_MARKER = '¨'
UMLAUTS = {'au': 'äu', 'a': 'ä', 'o': 'ö', 'u': 'ü'}
PLURAL_LABEL = 'plural'
PLURAL_SUFFIX_LABEL = 'plural_suffix'
FEMININE_LABEL = 'feminine'
VERB_LABELS_BY_LENGTH = {
    3: ('present', 'perfect'),
    4: ('present', 'preterite', 'perfect'),
}

SOURCE_NAME = 'goethe'
OUTPUT_FILENAME = 'words.tsv'

PARENTHETICAL_PATTERN = re.compile(r'\s*\(([^()]*)\)')
# A regional entry points at the standard form it stands in for; both halves go with it.
CROSS_REFERENCE_PATTERN = re.compile(r'\s*→.*$')
PAIR_SEPARATOR = ' / '
VARIANT_SEPARATOR = '/'
FORM_SEPARATOR = ', '
# A masculine/feminine pair is printed either side of a slash, and sometimes with nothing but a
# space between the two article-led halves; without this the feminine is read as a plural suffix.
PAIRED_NOUN_PATTERN = re.compile(r'\s+(?=(?:der|die|das)\s)')
EXAMPLE_NUMBER_PATTERN = re.compile(r'(?:(?<=\s)|^)(\d+)\.\s')
# Austrian and Swiss standard variants; the deck teaches the German one.
REGIONAL_MARKERS = ('A', 'CH')
PLURAL_ONLY_MARKER = 'Pl.'
# A hyphen before one of these stands for a compound left out, not for a line break.
SUSPENDED_HYPHEN_WORDS = frozenset({'und', 'oder', 'bis', 'sowie'})
# The plural dash of an entry printed wide enough to spill into the example column.
EXAMPLE_MIN_LETTERS = 3


class Column(typing.NamedTuple):
    """One printed column, as the x range of its headwords and the x range of its examples."""

    start: float
    examples_start: float
    end: float


class ListLayout(typing.NamedTuple):
    """Where one published list keeps its alphabetical entries.

    Page ranges and column edges were measured off the published files: the alphabetical list
    is the run of pages whose headwords all begin at the same x, and the front matter and the
    thematic list before it are laid out as prose and paragraphs instead. They are stated rather
    than detected because a wrong guess would silently drop or invent entries; the entry count
    is asserted afterwards so a re-issued file cannot pass unnoticed.
    """

    level: str
    filename: str
    url: str
    first_page: int
    last_page: int
    columns: typing.Tuple[Column, ...]
    # Everything above the running head and below the page number is furniture.
    body_top: float = 60.0
    body_bottom: float = 780.0
    expected_entries: typing.Tuple[int, int] = (0, 100000)
    # Where the headwords are short enough never to wrap, the extractor groups a whole run of
    # them into one block, so the line is the entry. Where they wrap — a verb printed with its
    # principal parts — only the block holds the wrapped halves together.
    lines_are_entries: bool = False


BASE_URL = 'https://www.goethe.de/pro/relaunch/prf/de/'
# Ordered lowest level first: each list is cumulative, so the first one holding a word names
# the level at which a learner meets it.
LISTS = (
    ListLayout(
        level='A1',
        filename='A1_SD1_Wortliste_02.pdf',
        url=BASE_URL + 'A1_SD1_Wortliste_02.pdf',
        first_page=9,
        last_page=26,
        columns=(Column(start=140.0, examples_start=235.0, end=595.0),),
        lines_are_entries=True,
        expected_entries=(500, 800),
    ),
    ListLayout(
        level='A2',
        filename='Goethe-Zertifikat_A2_Wortliste.pdf',
        url=BASE_URL + 'Goethe-Zertifikat_A2_Wortliste.pdf',
        first_page=8,
        last_page=31,
        columns=(
            Column(start=30.0, examples_start=105.0, end=300.0),
            Column(start=300.0, examples_start=375.0, end=595.0),
        ),
        expected_entries=(900, 1600),
    ),
    ListLayout(
        level='B1',
        filename='Goethe-Zertifikat_B1_Wortliste.pdf',
        url=BASE_URL + 'Goethe-Zertifikat_B1_Wortliste.pdf',
        first_page=16,
        last_page=102,
        columns=(
            Column(start=30.0, examples_start=125.0, end=300.0),
            Column(start=300.0, examples_start=400.0, end=595.0),
        ),
        # The foreword counts about 2400 main entries; the indented derivations beside them are
        # entries too and a learner needs each of them, so the real total runs higher.
        expected_entries=(2500, 3200),
    ),
)
# The richest list is also the widest, so it supplies the entries and the others only the level.
ENTRY_LIST_LEVEL = 'B1'
# Two blocks belong to the same entry when the printer put them on the same line.
SAME_LINE_TOLERANCE = 3.0


class Entry(typing.NamedTuple):
    """One printed entry before its headword is taken apart."""

    headword: str
    examples: typing.List[str]


class PdfExtractor:
    """Turns a published list into entries, using where the words sit on the page."""

    @staticmethod
    def download(*, layout: ListLayout, directory: pathlib.Path) -> pathlib.Path:
        path = directory / layout.filename
        if path.exists():
            return path
        request = urllib.request.Request(layout.url, headers={'User-Agent': USER_AGENT})
        path.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(request, timeout=60) as response:
            path.write_bytes(response.read())
        return path

    @staticmethod
    def extract(*, pdf_path: pathlib.Path) -> pathlib.Path:
        xml_path = pdf_path.with_suffix('.xml')
        if not xml_path.exists():
            subprocess.run(
                [PDFTOTEXT, '-bbox-layout', str(pdf_path), str(xml_path)],
                check=True, capture_output=True,
            )
        return xml_path

    @staticmethod
    def entries(*, layout: ListLayout, xml_path: pathlib.Path) -> typing.List[Entry]:
        tree = ElementTree.parse(xml_path)
        pages = list(tree.iter(XHTML_NAMESPACE + 'page'))
        found: typing.List[Entry] = []
        for number in range(layout.first_page, layout.last_page + 1):
            page = pages[number - 1]
            for column in layout.columns:
                found.extend(PdfExtractor._column_entries(page=page, column=column, layout=layout))
        return found

    @staticmethod
    def _column_entries(
        *, page: ElementTree.Element, column: Column, layout: ListLayout,
    ) -> typing.List[Entry]:
        """One column's entries, top to bottom.

        A block carrying headword-band words opens an entry; a block of nothing but example-band
        words belongs to whichever entry the printer put on the same line, and to the entry above
        it when the printer wrapped it onto its own.
        """
        entries: typing.List[Entry] = []
        positions: typing.List[float] = []
        for top, headword, example in PdfExtractor._blocks(page=page, column=column, layout=layout):
            if headword:
                entries.append(Entry(headword=headword, examples=PdfExtractor._split_examples(text=example)))
                positions.append(top)
                continue
            if not example or not entries:
                continue
            index = PdfExtractor._entry_at(positions=positions, top=top)
            entries[index].examples.extend(PdfExtractor._split_examples(text=example))
        return entries

    @staticmethod
    def _blocks(
        *, page: ElementTree.Element, column: Column, layout: ListLayout,
    ) -> typing.List[typing.Tuple[float, str, str]]:
        """Each block of this column as (top, headword text, example text).

        Splitting by word rather than by block is what makes this work: the extractor merges a
        short headword with its example often enough that a block-level rule would read the two
        as one headword and lose the example.
        """
        tag = 'line' if layout.lines_are_entries else 'block'
        units = []
        for unit in page.iter(XHTML_NAMESPACE + tag):
            top = float(unit.get('yMin'))
            if not layout.body_top <= top <= layout.body_bottom:
                continue
            headword_lines, example_lines = [], []
            for line in unit.iter(XHTML_NAMESPACE + 'line') if tag == 'block' else (unit,):
                headword_words, example_words = [], []
                for word in line.iter(XHTML_NAMESPACE + 'word'):
                    left = float(word.get('xMin'))
                    if not column.start <= left < column.end:
                        continue
                    text = (word.text or '').strip()
                    if not text:
                        continue
                    (headword_words if left < column.examples_start else example_words).append(text)
                if headword_words:
                    headword_lines.append(' '.join(headword_words))
                if example_words:
                    example_lines.append(' '.join(example_words))
            if headword_lines or example_lines:
                units.append((
                    top,
                    PdfExtractor._join_wrapped(lines=headword_lines),
                    PdfExtractor._join_wrapped(lines=example_lines),
                ))
        return sorted(units, key=lambda unit: unit[0])

    @staticmethod
    def _join_wrapped(*, lines: typing.List[str]) -> str:
        """Rejoin a word the typesetter broke across two lines.

        The break leaves a hyphen that belongs to neither half, so joining the lines with a space
        puts "Hausauf- gaben" on the card. A hyphen at the end of a line is only a break when it
        hangs off a word and a lowercase word follows it. On its own it is a noun's plural, and
        before "und" or "oder" it stands for a whole omitted compound; gluing either together
        destroys the entry.
        """
        joined = ''
        for line in lines:
            if not joined:
                joined = line
                continue
            head = joined.rsplit(' ', 1)[-1]
            follower = line.split(' ', 1)[0]
            if (
                len(head) > 1 and head.endswith('-')
                and follower[:1].islower() and follower not in SUSPENDED_HYPHEN_WORDS
            ):
                joined = joined[:-1] + line
            else:
                joined = f'{joined} {line}'
        return joined

    @staticmethod
    def _entry_at(*, positions: typing.List[float], top: float) -> int:
        for index in range(len(positions) - 1, -1, -1):
            if abs(positions[index] - top) <= SAME_LINE_TOLERANCE:
                return index
        return len(positions) - 1

    @staticmethod
    def _split_examples(*, text: str) -> typing.List[str]:
        """A numbered run of sentences, split on its numbering and not on the dates inside it.

        A marker counts only when it continues the numbering, so "Ab 1. Juli" inside the first
        sentence cannot open a second one.
        """
        text = text.strip()
        if not text:
            return []
        cuts, expected = [], 1
        for match in EXAMPLE_NUMBER_PATTERN.finditer(text):
            if int(match.group(1)) != expected:
                continue
            cuts.append((match.start(), match.end()))
            expected += 1
        if not cuts:
            return [text] if PdfExtractor._is_sentence(text=text) else []
        bounds = [end for _, end in cuts] + [len(text)]
        starts = [start for start, _ in cuts[1:]] + [len(text)]
        return [
            sentence for sentence in (text[begin:finish].strip() for begin, finish in zip(bounds, starts))
            if PdfExtractor._is_sentence(text=sentence)
        ]

    @staticmethod
    def _is_sentence(*, text: str) -> bool:
        return sum(character.isalpha() for character in text) >= EXAMPLE_MIN_LETTERS


class HeadwordParser:
    """Takes a printed headword apart into the columns a card is built from."""

    @staticmethod
    def is_regional(*, headword: str) -> bool:
        return any(f'({marker})' in headword for marker in REGIONAL_MARKERS)

    @staticmethod
    def parse(*, headword: str) -> typing.Optional[typing.Dict[str, typing.Any]]:
        text = CROSS_REFERENCE_PATTERN.sub('', headword).strip()
        number = 'pl' if f'({PLURAL_ONLY_MARKER})' in text else ''
        text = PARENTHETICAL_PATTERN.sub('', text).strip()
        # An alphabet heading and a stray page number are printed in the headword column too.
        if not text or len(text) < 2 or text.isdigit():
            return None

        forms: typing.Dict[str, typing.List[str]] = {}
        primary, _, paired = text.partition(PAIR_SEPARATOR)
        halves = PAIRED_NOUN_PATTERN.split(primary.strip())
        primary = halves[0].strip()
        paired = paired.strip() or (halves[1].strip() if len(halves) > 1 else '')

        parsed = HeadwordParser._as_verb(text=primary) or HeadwordParser._as_noun(text=primary)
        if parsed is None:
            # A word printed with a spelling variant beside it: the first is the one to learn.
            parsed = {
                'word': primary.partition(',')[0].strip(),
                'article': '', 'gender': '', 'part_of_speech': '',
            }
        forms.update(parsed.pop('forms', {}))

        feminine = HeadwordParser._feminine_pair(article=parsed['article'], paired=paired.strip())
        if feminine:
            forms[FEMININE_LABEL] = [feminine]

        # A spelling or regional variant printed after a slash — Soße/Sauce, chic/schick. The
        # first is the one the deck teaches; the slash has to go either way, because a headword
        # carrying it answers to no dictionary and to no image search.
        parsed['word'] = parsed['word'].split(VARIANT_SEPARATOR, 1)[0].strip()
        parsed['number'] = number
        parsed['forms'] = forms
        return parsed if parsed['word'] else None

    @staticmethod
    def _as_verb(*, text: str) -> typing.Optional[typing.Dict[str, typing.Any]]:
        parts = [part.strip() for part in text.split(FORM_SEPARATOR)]
        labels = VERB_LABELS_BY_LENGTH.get(len(parts))
        if labels is None or not all(parts):
            return None
        if not parts[-1].split()[0] in AUXILIARIES:
            return None
        return {
            'word': parts[0],
            'article': '',
            'gender': '',
            'part_of_speech': 'verb',
            'forms': {label: [part] for label, part in zip(labels, parts[1:])},
        }

    @staticmethod
    def _as_noun(*, text: str) -> typing.Optional[typing.Dict[str, typing.Any]]:
        head, _, suffix = text.partition(',')
        words = head.split()
        if len(words) < 2 or words[0].lower() not in ARTICLE_GENDERS:
            return None
        article = words[0].lower()
        singular = ' '.join(words[1:])
        forms: typing.Dict[str, typing.List[str]] = {}
        suffix = suffix.strip()
        if suffix:
            forms[PLURAL_SUFFIX_LABEL] = [suffix]
            plural = HeadwordParser.expand_plural(singular=singular, suffix=suffix)
            if plural:
                forms[PLURAL_LABEL] = [plural]
        return {
            'word': singular,
            'article': article,
            'gender': ARTICLE_GENDERS[article],
            'part_of_speech': 'noun',
            'forms': forms,
        }

    @staticmethod
    def expand_plural(*, singular: str, suffix: str) -> str:
        """The plural as the learner has to say it, not as the list abbreviates it.

        The printed form is a suffix with an optional umlaut mark standing for the stem change,
        and a card showing `¨-e` teaches nothing — the learner needs `Märkte`.
        """
        umlauted = suffix.startswith(UMLAUT_MARKER)
        tail = suffix.lstrip(UMLAUT_MARKER)
        if not tail.startswith('-'):
            return ''
        tail = tail[1:]
        stem = HeadwordParser.apply_umlaut(word=singular) if umlauted else singular
        if not stem:
            return ''
        # A suffix that is itself a capitalised word replaces the singular rather than extending it.
        return tail if tail[:1].isupper() else stem + tail

    @staticmethod
    def apply_umlaut(*, word: str) -> str:
        """Umlaut the last stem vowel, treating `au` as the single vowel it is.

        Scanning backwards reaches the `u` of `au` before the digraph itself, so the digraph is
        tested from the character before — otherwise Haus becomes Haüser instead of Häuser.
        """
        for position in range(len(word) - 1, -1, -1):
            pair = word[position - 1:position + 1].lower() if position else ''
            if pair in UMLAUTS:
                return word[:position - 1] + HeadwordParser._match_case(
                    replacement=UMLAUTS[pair], original=word[position - 1],
                ) + word[position + 1:]
            single = word[position].lower()
            if single in UMLAUTS:
                return word[:position] + HeadwordParser._match_case(
                    replacement=UMLAUTS[single], original=word[position],
                ) + word[position + 1:]
        return ''

    @staticmethod
    def _match_case(*, replacement: str, original: str) -> str:
        return replacement.capitalize() if original.isupper() else replacement

    @staticmethod
    def _feminine_pair(*, article: str, paired: str) -> str:
        """The `der X / die Xin` pair the lists print as one entry.

        The feminine is formed regularly and does not deserve a card of its own, but a learner
        still has to produce it, so it rides along on the masculine's card as a form.
        """
        if article != 'der' or not paired.lower().startswith('die '):
            return ''
        return paired


class GoetheImporter:
    """Builds the German word table out of the published lists."""

    def __init__(self, *, language: language_config.LanguageConfig, root: pathlib.Path):
        self._language = language
        self._root = root
        self._cache = language.data_directory(root=root) / CACHE_DIRECTORY

    def read_list(self, *, layout: ListLayout) -> typing.List[Entry]:
        pdf_path = PdfExtractor.download(layout=layout, directory=self._cache)
        xml_path = PdfExtractor.extract(pdf_path=pdf_path)
        entries = PdfExtractor.entries(layout=layout, xml_path=xml_path)
        low, high = layout.expected_entries
        assert low <= len(entries) <= high, (
            f"{layout.level}: read {len(entries)} entries, expected between {low} and {high} — "
            f"the published file's layout has probably changed"
        )
        return entries

    @staticmethod
    def lemmas(*, entries: typing.List[Entry]) -> typing.Set[str]:
        """A list's headwords reduced to what a level lookup can match on."""
        found = set()
        for entry in entries:
            parsed = HeadwordParser.parse(headword=entry.headword)
            if parsed is not None:
                found.add(parsed['word'].casefold())
        return found

    def build(self) -> typing.Tuple[typing.List[dict], typing.Dict[str, int]]:
        by_level = {layout.level: self.read_list(layout=layout) for layout in LISTS}
        lower_levels = [
            (layout.level, GoetheImporter.lemmas(entries=by_level[layout.level]))
            for layout in LISTS if layout.level != ENTRY_LIST_LEVEL
        ]

        rows: typing.List[dict] = []
        seen: typing.Set[str] = set()
        counts = {'regional': 0, 'unparsed': 0, 'duplicate': 0, 'noun': 0, 'verb': 0, 'other': 0}
        for level in by_level:
            counts[level] = 0

        for entry in by_level[ENTRY_LIST_LEVEL]:
            if HeadwordParser.is_regional(headword=entry.headword):
                counts['regional'] += 1
                continue
            parsed = HeadwordParser.parse(headword=entry.headword)
            if parsed is None:
                counts['unparsed'] += 1
                continue
            row = self._build_row(parsed=parsed, examples=entry.examples, lower_levels=lower_levels)
            if row['key'] in seen:
                counts['duplicate'] += 1
                continue
            seen.add(row['key'])
            rows.append(row)
            counts[row['cefr']] += 1
            counts[parsed['part_of_speech'] or 'other'] += 1
        return rows, counts

    def _build_row(
        self,
        *,
        parsed: typing.Dict[str, typing.Any],
        examples: typing.List[str],
        lower_levels: typing.List[typing.Tuple[str, typing.Set[str]]],
    ) -> dict:
        language = self._language
        row = {column: '' for column in language.extracted_columns}
        row['key'] = reword.WordExporter.build_key(
            article=parsed['article'], word=parsed['word'], translations_native='',
        )
        row['word'] = parsed['word']
        row['article'] = parsed['article']
        row['gender'] = parsed['gender']
        row['part_of_speech'] = parsed['part_of_speech']
        row['number'] = parsed['number']
        row['cefr'] = self._level_of(word=parsed['word'], lower_levels=lower_levels)
        row['level_source'] = SOURCE_NAME
        if parsed['forms']:
            row['inflection'] = json.dumps(parsed['forms'], ensure_ascii=False)
        if examples:
            self._write_examples(row=row, examples=examples)
        return row

    @staticmethod
    def _level_of(
        *, word: str, lower_levels: typing.List[typing.Tuple[str, typing.Set[str]]],
    ) -> str:
        folded = word.casefold()
        for level, lemmas in lower_levels:
            if folded in lemmas:
                return level
        return ENTRY_LIST_LEVEL

    def _write_examples(self, *, row: dict, examples: typing.List[str]) -> None:
        """The sentences, with the translation columns padded to the same number of lines.

        Cards render the columns side by side, so a translation column that is short by a line
        pairs every later sentence with the wrong gloss. The lists gloss nothing, so the padding
        is all this stage can supply; the translation stage fills the lines by number later.
        """
        language = self._language
        sentences = [reword.WordExporter.clean(value=sentence) for sentence in examples]
        sentences = [sentence for sentence in sentences if sentence]
        if not sentences:
            return
        row[language.examples_column(suffix=language.target)] = reword.WordExporter.join_lines(sentences)
        blank = '<br>' * (len(sentences) - 1)
        row[language.examples_column(suffix=language.native)] = blank
        if language.pivot is not None:
            row[language.examples_column(suffix=language.pivot)] = blank
        row['examples_source'] = SOURCE_NAME


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--replace', action='store_true',
        help="Discard the existing words.tsv instead of appending the new words to it",
    )
    language_config.add_language_arguments(parser)
    arguments = parser.parse_args()

    root = language_config.PROJECT_ROOT
    language = language_config.language_from(arguments, root=root)
    output_path = language.data_directory(root=root) / OUTPUT_FILENAME

    importer = GoetheImporter(language=language, root=root)
    rows, counts = importer.build()

    existing = []
    if output_path.exists() and not arguments.replace:
        existing = language_config.TsvFile.read(output_path)
    by_key = {row['key'] for row in existing}
    added = [row for row in rows if row['key'] not in by_key]
    merged = existing + added

    language_config.TsvFile.write(output_path, rows=merged, columns=language.extracted_columns)

    levels = ' | '.join(f"{layout.level} {counts[layout.level]}" for layout in LISTS)
    print(f"levels: {levels}", file=sys.stderr)
    print(
        f"parts of speech: noun {counts['noun']} | verb {counts['verb']} | other {counts['other']}",
        file=sys.stderr,
    )
    print(
        f"skipped: {counts['regional']} regional variants, {counts['unparsed']} unreadable, "
        f"{counts['duplicate']} duplicate keys",
        file=sys.stderr,
    )
    print(f"added {len(added)} new words to {len(existing)} already there — {len(merged)} in {output_path}")


if __name__ == '__main__':
    main()
