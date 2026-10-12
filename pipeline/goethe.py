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
REFLEXIVE_WORDS = frozenset({'sich'})
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
# Marks after which a headword runs on to the next line: more forms, a pair, a regional form.
CONTINUATION_ENDINGS = (',', '/', '→', ':', ';')
CONTINUATION_START_PATTERN = re.compile(r'^(?:→|\(|/|(?:D|A|CH)[,:])')
FIRST_EXAMPLE_MARKER = '1.'
SENTENCE_ENDINGS = ('.', '!', '?')
# A verb entry opens with its infinitive and a comma.
VERB_START_PATTERN = re.compile(r'^[a-zäöü]\S*,')
# How many letters a feminine shares with its masculine: Lehrer/Lehrerin, Arzt/Ärztin.
PAIR_STEM_LENGTH = 3
UMLAUT_FOLDING = str.maketrans({'ä': 'a', 'ö': 'o', 'ü': 'u'})
ARTICLE_START_PATTERN = re.compile(r'^(?:der|die|das)(?:/(?:der|die|das))?\s')
# An optional part printed in brackets inside a word, as in (Back-)Ofen; the bracket goes, the
# space before it stays.
OPTIONAL_PART_PATTERN = re.compile(r'\([\w-]+-\)(?=\w)')
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
# An entry's examples may start one printed line below its headword.
LINE_HEIGHT = 11.0


class Entry(typing.NamedTuple):
    """One printed entry before its headword is taken apart."""

    headword: str
    examples: typing.List[str]


class Block(typing.NamedTuple):
    """A headword with the example lines printed beside it, or example lines on their own."""

    top: float
    headword: str
    example_lines: typing.List[typing.Tuple[float, str]]


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
        blocks = PdfExtractor._blocks(page=page, column=column, layout=layout)
        entries: typing.List[Entry] = []
        positions: typing.List[float] = []
        for block in blocks:
            if block.headword:
                example = PdfExtractor._join_wrapped(lines=[text for _, text in block.example_lines])
                entries.append(Entry(headword=block.headword, examples=PdfExtractor._split_examples(text=example)))
                positions.append(block.top)
        # A second pass: an example block often starts a little above its own headword, so it
        # is sorted first and would land on the previous entry.
        for block in blocks:
            if block.headword or not block.example_lines or not entries:
                continue
            for index, lines in PdfExtractor._lines_by_entry(block=block, positions=positions).items():
                example = PdfExtractor._join_wrapped(lines=lines)
                entries[index].examples.extend(PdfExtractor._split_examples(text=example))
        return entries

    @staticmethod
    def _lines_by_entry(
        *, block: Block, positions: typing.List[float],
    ) -> typing.Dict[int, typing.List[str]]:
        """Share one example block between entries: a new sentence on an entry's line is its own."""
        owner = PdfExtractor._entry_at(positions=positions, top=block.example_lines[0][0])
        by_entry: typing.Dict[int, typing.List[str]] = {}
        previous = ''
        for top, text in block.example_lines:
            beside = PdfExtractor._entry_on_line(positions=positions, top=top)
            opens_sentence = not previous or (
                previous.endswith(SENTENCE_ENDINGS) and (text[:1].isupper() or text[:1].isdigit())
            )
            if beside is not None and opens_sentence:
                owner = beside
            if owner is not None:
                by_entry.setdefault(owner, []).append(text)
            previous = text
        return by_entry

    @staticmethod
    def _blocks(
        *, page: ElementTree.Element, column: Column, layout: ListLayout,
    ) -> typing.List[Block]:
        """Each entry or example block of this column, top to bottom.

        Splitting by word rather than by block is what makes this work: the extractor merges a
        short headword with its example often enough that a block-level rule would read the two
        as one headword and lose the example.
        """
        tag = 'line' if layout.lines_are_entries else 'block'
        example_starts = PdfExtractor._example_starts(page=page, column=column)
        units = []
        for unit in page.iter(XHTML_NAMESPACE + tag):
            top = float(unit.get('yMin'))
            if not layout.body_top <= top <= layout.body_bottom:
                continue
            headword_lines: typing.List[typing.Tuple[float, str]] = []
            example_lines: typing.List[typing.Tuple[float, str]] = []
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
                    headword_lines.append((float(line.get('yMin')), ' '.join(headword_words)))
                if example_words:
                    example_lines.append((float(line.get('yMin')), ' '.join(example_words)))
            groups = PdfExtractor._group_entries(lines=headword_lines, example_starts=example_starts)
            if not groups:
                if example_lines:
                    units.append(Block(top=top, headword='', example_lines=example_lines))
                continue
            lines_by_group: typing.List[typing.List[typing.Tuple[float, str]]] = [[] for _ in groups]
            for example_line in example_lines:
                owner = 0
                for index, (group_top, _) in enumerate(groups):
                    if group_top <= example_line[0] + SAME_LINE_TOLERANCE:
                        owner = index
                lines_by_group[owner].append(example_line)
            for index, (group_top, group_lines) in enumerate(groups):
                units.append(Block(
                    top=top if index == 0 else group_top,
                    headword=PdfExtractor._join_wrapped(lines=group_lines),
                    example_lines=lines_by_group[index],
                ))
        return sorted(units, key=lambda unit: unit.top)

    @staticmethod
    def _example_starts(*, page: ElementTree.Element, column: Column) -> typing.List[float]:
        """Tops of the lines where an entry's examples begin: a block's first line, or a "1."."""
        starts = []
        for block in page.iter(XHTML_NAMESPACE + 'block'):
            for position, line in enumerate(block.iter(XHTML_NAMESPACE + 'line')):
                words = [
                    (word.text or '').strip() for word in line.iter(XHTML_NAMESPACE + 'word')
                    if column.examples_start <= float(word.get('xMin')) < column.end
                ]
                words = [word for word in words if word]
                if words and (position == 0 or words[0] == FIRST_EXAMPLE_MARKER):
                    starts.append(float(line.get('yMin')))
        return starts

    @staticmethod
    def _group_entries(
        *, lines: typing.List[typing.Tuple[float, str]], example_starts: typing.List[float],
    ) -> typing.List[typing.Tuple[float, typing.List[str]]]:
        """Split one block's headword lines into entries; the extractor sometimes merges two."""
        groups: typing.List[typing.Tuple[float, typing.List[str]]] = []
        for top, text in lines:
            opens = groups and PdfExtractor._starts_entry(
                entry=' '.join(groups[-1][1]), line=text,
                beside_example=any(
                    groups[-1][0] + SAME_LINE_TOLERANCE < start
                    and top - SAME_LINE_TOLERANCE <= start <= top + LINE_HEIGHT + SAME_LINE_TOLERANCE
                    for start in example_starts
                ),
            )
            if groups and not opens:
                groups[-1][1].append(text)
            else:
                groups.append((top, [text]))
        return groups

    @staticmethod
    def _starts_entry(*, entry: str, line: str, beside_example: bool) -> bool:
        """Whether a headword line opens a new entry rather than continuing the one above.

        An entry goes on after a list mark, inside open brackets and after an auxiliary. A new one
        starts with an article under a non-noun, or on the line where its examples start.
        """
        last_word = entry.rsplit(' ', 1)[-1]
        if (
            entry.endswith(CONTINUATION_ENDINGS) or CONTINUATION_START_PATTERN.match(line)
            or entry.count('(') > entry.count(')') or last_word in AUXILIARIES
        ):
            return False
        follower = line.split(' ', 1)[0]
        hyphen_break = (
            len(last_word) > 1 and last_word.endswith('-') and last_word[-2].isalpha()
            and follower[:1].islower() and follower not in SUSPENDED_HYPHEN_WORDS
            and not ARTICLE_START_PATTERN.match(line)
        )
        # A lone prefix such as "über-" is an entry of its own, not the first half of a word.
        if hyphen_break and (' ' in entry or not beside_example):
            return False
        if ARTICLE_START_PATTERN.match(line):
            return not PdfExtractor._is_feminine_pair(entry=entry, line=line)
        if VERB_START_PATTERN.match(line) and not PdfExtractor._is_unfinished_verb(entry=entry):
            return True
        return beside_example

    @staticmethod
    def _is_feminine_pair(*, entry: str, line: str) -> bool:
        """`der Lehrer, -` over `die Lehrerin, -nen` is one entry; any other noun under a noun is not."""
        if not entry.startswith('der ') or not line.startswith('die '):
            return False
        masculine = PdfExtractor._fold(text=entry.split(' ', 2)[1])
        feminine = PdfExtractor._fold(text=line.split(' ', 2)[1])
        return feminine.startswith(masculine[:PAIR_STEM_LENGTH])

    @staticmethod
    def _fold(*, text: str) -> str:
        return text.casefold().translate(UMLAUT_FOLDING)

    @staticmethod
    def _is_unfinished_verb(*, entry: str) -> bool:
        """A verb whose forms have started but whose perfect (auxiliary and participle) has not."""
        parts = entry.split(FORM_SEPARATOR)
        if len(parts) < 2 or ARTICLE_START_PATTERN.match(entry):
            return False
        last = parts[-1].split()
        return not (any(word in AUXILIARIES for word in last) and last[-1] not in AUXILIARIES)

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
    def _entry_on_line(*, positions: typing.List[float], top: float) -> typing.Optional[int]:
        for index, position in enumerate(positions):
            if abs(position - top) <= SAME_LINE_TOLERANCE:
                return index
        return None

    @staticmethod
    def _entry_at(*, positions: typing.List[float], top: float) -> typing.Optional[int]:
        """The entry on the same line, else the last entry above: a wrapped example continues it."""
        beside = PdfExtractor._entry_on_line(positions=positions, top=top)
        if beside is not None:
            return beside
        above = [index for index, position in enumerate(positions) if position < top]
        return above[-1] if above else None

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
        text = OPTIONAL_PART_PATTERN.sub('', headword)
        text = CROSS_REFERENCE_PATTERN.sub('', text).strip()
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
        variants = [variant.strip() for variant in parsed['word'].split(VARIANT_SEPARATOR)]
        # "Nord-/Ostsee" shares a tail; the second variant is the one spelled out in full.
        spelled_out = len(variants) > 1 and variants[0].endswith('-') and '-' not in variants[1]
        parsed['word'] = variants[1] if spelled_out else variants[0]
        parsed['number'] = number
        parsed['forms'] = forms
        return parsed if parsed['word'] else None

    @staticmethod
    def _as_verb(*, text: str) -> typing.Optional[typing.Dict[str, typing.Any]]:
        parts = [part.strip().rstrip('.') for part in text.split(FORM_SEPARATOR)]
        parts = HeadwordParser._restore_missing_comma(parts=parts)
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
    def _restore_missing_comma(*, parts: typing.List[str]) -> typing.List[str]:
        """`festnehmen nimmt fest, nahm fest, hat festgenommen`: the list dropped one comma."""
        words = parts[0].split()
        if len(parts) != min(VERB_LABELS_BY_LENGTH) or len(words) < 2:
            return parts
        if not words[0][:1].islower() or words[0] in REFLEXIVE_WORDS or words[1] in REFLEXIVE_WORDS:
            return parts
        # The glued present and the preterite end with the same separable particle.
        if words[-1] != parts[1].split()[-1]:
            return parts
        return [words[0], ' '.join(words[1:])] + parts[1:]

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
