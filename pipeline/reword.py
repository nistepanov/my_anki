"""Export a ReWord SQLite backup into a TSV skeleton ready for Anki cards.

Columns the backup cannot provide (definition, synonyms, antonyms, CEFR level,
the object/abstract tag) are emitted empty and filled by a later enrichment pass.

Language-neutral: the target, native and pivot languages come from a config
under languages/, mirroring the reader in model.py so both stages agree
on column names.
"""

import argparse
import contextlib
import functools
import json
import pathlib
import re
import sqlite3
import typing

from . import language_config

POS_BITS = {
    0: 'noun',
    1: 'verb',
    2: 'adjective',
    3: 'adverb',
    4: 'pronoun',
    5: 'preposition',
    6: 'conjunction',
    7: 'interjection',
    8: 'article',
    9: 'numeral',
    10: 'particle',
    11: 'participle',
    14: 'abbreviation',
}
GENDER_BITS = {12: 'm', 13: 'f'}
PLURAL_BIT = 15

# ReWord wraps the studied word in the example with hashes.
TARGET_WORD_PATTERN = re.compile(r'#([^#]*)#')
# The transcription is stored as '[article] [ipa]'; only the last group is the word itself.
TRANSCRIPTION_PATTERN = re.compile(r'\[([^\]]*)\]')

FREQUENCY_CATEGORIES = ('top100', 'top1000', 'top3000', 'top5000')
# Some vocabulary lists ship an official level list of their own, as categories named after the
# level they hold. Where one does, it answers the very question the model is otherwise asked to
# guess, so it is read as a level rather than filed away as a topic.
CEFR_CATEGORY_PATTERN = re.compile(r'[_-](a1|a2|b1|b2|c1|c2)$', re.IGNORECASE)
CEFR_LEVELS_LOW_TO_HIGH = ('A1', 'A2', 'B1', 'B2', 'C1', 'C2')
DEFAULT_CATEGORIES = ('top1000', 'custom')
# Native translations are comma-separated senses; also used to build the first-sense key.
TRANSLATION_SEPARATOR = ','
ENRICHED_COLUMN_NAMES = ('synonyms', 'antonyms', 'cefr', 'is_object')


@functools.lru_cache(maxsize=None)
def leading_particle_pattern(particles: typing.Tuple[str, ...]) -> typing.Optional[typing.Pattern]:
    """Matches whatever the language glues in front of a headword, longest first.

    An article for most languages; for English it is the infinitive marker, which is the same
    problem wearing a different name — a word left carrying it is a word no dictionary answers to.
    """
    if not particles:
        return None
    alternatives = '|'.join(re.escape(particle) for particle in sorted(particles, key=len, reverse=True))
    return re.compile(rf'^({alternatives})\s+', re.IGNORECASE)


# A source list is written by people and carries their slips: a misspelt headword teaches the
# misspelling, and a word the learner should not be drilling teaches worse than nothing. Which
# ones those are is a fact about one list, not about the language, so it lives beside the list.
PRINCIPAL_PARTS_SEPARATOR = ' - '
PRINCIPAL_PART_LABELS = ('past', 'past participle')


class SourceFixes(typing.NamedTuple):
    """Corrections to a vocabulary list, kept as data so re-exporting cannot lose them."""

    rename: typing.Mapping[str, str] = {}
    drop: typing.FrozenSet[str] = frozenset()

    @classmethod
    def load(cls, *, code: str, root: pathlib.Path) -> 'SourceFixes':
        path = root / 'words' / f'{code}-fixes.json'
        if not path.exists():
            return cls()
        payload = json.loads(path.read_text(encoding='utf-8'))
        return cls(
            rename={key.lower(): value for key, value in payload.get('rename', {}).items()},
            drop=frozenset(word.lower() for word in payload.get('drop', ())),
        )

    def apply(self, *, word: str) -> typing.Optional[str]:
        """The corrected headword, or None when the list should not carry this word at all."""
        lowered = word.lower()
        if lowered in self.drop:
            return None
        return self.rename.get(lowered, word)


class BackupColumns(typing.NamedTuple):
    """Which of the backup's translation columns this deck reads.

    The app names a translation column after the language's ISO 639-3 code in capitals, and
    ships no column at all for the language being learned — that one is the headword. So two
    backups of the same app hold different columns depending on which language each was for,
    and the names have to be resolved against the file rather than assumed.
    """

    native_translation: str
    native_examples: str
    pivot_translation: str = ''
    pivot_examples: str = ''

    @classmethod
    def resolve(
        cls, *, connection: sqlite3.Connection, language: language_config.LanguageConfig,
    ) -> 'BackupColumns':
        available = {record[1] for record in connection.execute('PRAGMA table_info(WORD)')}

        def column(*, suffix: str, prefix: str = '') -> str:
            code = language_config.LANGUAGE_FACTS.get(suffix, {}).get('iso3', suffix)
            name = f'{prefix}{code.upper()}'
            return name if name in available else ''

        native_translation = column(suffix=language.native)
        if not native_translation:
            raise SystemExit(
                f"the backup carries no {language.native_name} translations — "
                f"it was exported for a different pair of languages"
            )
        pivot = language.pivot
        return cls(
            native_translation=native_translation,
            native_examples=column(suffix=language.native, prefix='EXAMPLES_'),
            pivot_translation=column(suffix=pivot) if pivot is not None else '',
            pivot_examples=column(suffix=pivot, prefix='EXAMPLES_') if pivot is not None else '',
        )


class WordExporter:
    """Reads words from a ReWord backup, collapses duplicate senses and renders TSV rows."""

    def __init__(
        self,
        *,
        connection: sqlite3.Connection,
        language: language_config.LanguageConfig,
        fixes: typing.Optional['SourceFixes'] = None,
    ):
        self._connection = connection
        self._language = language
        self._columns = BackupColumns.resolve(connection=connection, language=language)
        self._fixes = fixes if fixes is not None else SourceFixes()

    def select_word_ids(self, *, categories: typing.Iterable[str], include_studied: bool) -> typing.Set[int]:
        category_list = list(categories)
        placeholders = ','.join('?' * len(category_list))
        query = f'SELECT WORD_ID FROM WORD_CATEGORY WHERE CATEGORY_ID IN ({placeholders})'
        word_ids = {row[0] for row in self._connection.execute(query, category_list)}
        if include_studied:
            studied = self._connection.execute('SELECT ID FROM WORD WHERE S_REC > 0 OR S_REP > 0')
            word_ids |= {row[0] for row in studied}
        return word_ids

    def categories_by_word(self) -> typing.Dict[int, typing.List[str]]:
        grouped: typing.Dict[int, typing.List[str]] = {}
        for word_id, category_id in self._connection.execute('SELECT WORD_ID, CATEGORY_ID FROM WORD_CATEGORY'):
            grouped.setdefault(word_id, []).append(category_id)
        return grouped

    def pictures_in_backup(self) -> typing.Set[int]:
        query = 'SELECT ID FROM PICTURE WHERE CONTENT IS NOT NULL'
        return {row[0] for row in self._connection.execute(query)}

    def rows(self, *, word_ids: typing.Set[int]) -> typing.Tuple[typing.List[typing.Dict[str, str]], int]:
        """Fetch matching words, collapse duplicate senses and return (rows, merged_count)."""
        categories = self.categories_by_word()
        pictures = self.pictures_in_backup()
        raw_rows = list(self._fetch_raw_rows(word_ids=word_ids, categories=categories, pictures=pictures))

        merged_rows: typing.List[dict] = []
        merged_count = 0
        for group in self._group_by_bare_word(rows=raw_rows).values():
            for cluster in self._cluster_by_sense_overlap(rows=group):
                merged_rows.append(self._merge_cluster(members=cluster))
                merged_count += len(cluster) - 1

        for row in merged_rows:
            row['key'] = self.build_key(
                article=row['article'],
                word=row['word'],
                translations_native=row[self._language.translations_native_column],
            )
            row.pop('_category_ids', None)
            # A cell the source could fill is already filled; the rest wait for later stages.
            for column in self._language.enriched_columns:
                row.setdefault(column, '')

        self.assert_unique_keys(rows=merged_rows)
        return merged_rows, merged_count

    def _fetch_raw_rows(
        self,
        *,
        word_ids: typing.Set[int],
        categories: typing.Dict[int, typing.List[str]],
        pictures: typing.Set[int],
    ) -> typing.Iterator[dict]:
        language = self._language
        columns = self._columns
        selected = ['ID', 'WORD', 'TRANSCRIPTION', 'POS', 'PICTURE_ID', 'S_REC', 'S_REP', *(
            name for name in columns if name
        )]
        query = f'SELECT {", ".join(selected)} FROM WORD ORDER BY ID'
        for record in self._connection.execute(query):
            word_id = record['ID']
            if word_id not in word_ids:
                continue
            corrected = self._fixes.apply(word=self.clean(record['WORD']))
            if corrected is None:
                continue
            article, bare_word = self.split_article(
                word=corrected, particles=language.articles,
            )
            bare_word, principal_parts = self.split_principal_parts(word=bare_word)
            originals, native_lines, pivot_lines = self.align_examples(
                native=self.parse_examples(raw=self._cell(record=record, column=columns.native_examples)),
                pivot=self.parse_examples(raw=self._cell(record=record, column=columns.pivot_examples)),
            )
            row = {
                'word': bare_word,
                'article': article,
                language.translations_native_column: self.clean(record[columns.native_translation]),
                'ipa': self.extract_ipa(transcription=record['TRANSCRIPTION']),
                language.examples_column(suffix=language.target): self.join_lines(originals),
                language.examples_column(suffix=language.native): self.join_lines(native_lines),
                'status': 'studied' if record['S_REC'] > 0 or record['S_REP'] > 0 else 'new',
                'has_image': 'yes' if record['PICTURE_ID'] in pictures else '',
                '_category_ids': sorted(categories.get(word_id, [])),
            }
            if principal_parts:
                row['inflection'] = json.dumps(principal_parts, ensure_ascii=False)
            if language.pivot is not None:
                row[language.translations_pivot_column] = self.clean(
                    self._cell(record=record, column=columns.pivot_translation),
                )
                row[language.examples_column(suffix=language.pivot)] = self.join_lines(pivot_lines)
            row.update(self.decode_pos(pos=record['POS']))
            yield row

    @staticmethod
    def _cell(*, record: sqlite3.Row, column: str) -> typing.Optional[str]:
        """A column the backup does not carry reads as absent, not as an error."""
        return record[column] if column else None

    @staticmethod
    def _group_by_bare_word(*, rows: typing.List[dict]) -> typing.Dict[str, typing.List[dict]]:
        """The poorer 'custom' duplicates of a headword always drop its article, so
        the article cannot be part of the grouping key or they would never meet."""
        grouped: typing.Dict[str, typing.List[dict]] = {}
        for row in rows:
            grouped.setdefault(row['word'].lower(), []).append(row)
        return grouped

    def _cluster_by_sense_overlap(self, *, rows: typing.List[dict]) -> typing.List[typing.List[dict]]:
        """Union-find over comma-separated senses so overlap chains transitively."""
        native_column = self._language.translations_native_column
        parent = list(range(len(rows)))

        def find(index: int) -> int:
            while parent[index] != index:
                parent[index] = parent[parent[index]]
                index = parent[index]
            return index

        def union(first: int, second: int) -> None:
            root_first, root_second = find(first), find(second)
            if root_first != root_second:
                parent[root_first] = root_second

        senses = [self.sense_set(translations=row[native_column]) for row in rows]
        for first in range(len(rows)):
            for second in range(first + 1, len(rows)):
                if senses[first] & senses[second]:
                    union(first, second)

        clusters: typing.Dict[int, typing.List[dict]] = {}
        for index, row in enumerate(rows):
            clusters.setdefault(find(index), []).append(row)
        return list(clusters.values())

    def _merge_cluster(self, *, members: typing.List[dict]) -> dict:
        """Richest row wins as the base; every other cell missing on it is backfilled."""
        language = self._language
        target_examples_column = language.examples_column(suffix=language.target)
        base = max(members, key=lambda row: self.richness(row=row, target_examples_column=target_examples_column))
        merged = dict(base)

        fillable_columns = [
            'article', 'part_of_speech', 'gender', 'number', 'ipa', 'has_image', 'inflection',
            language.translations_native_column,
        ]
        if language.pivot is not None:
            fillable_columns.append(language.translations_pivot_column)
        for column in fillable_columns:
            if not merged.get(column):
                for other in members:
                    if other.get(column):
                        merged[column] = other[column]
                        break

        # The three example columns are index-aligned within one row; backfill them
        # together from a single donor so a sentence and its translations never mix.
        example_columns = [target_examples_column, language.examples_column(suffix=language.native)]
        if language.pivot is not None:
            example_columns.append(language.examples_column(suffix=language.pivot))
        if not merged.get(target_examples_column):
            for other in members:
                if other.get(target_examples_column):
                    for column in example_columns:
                        merged[column] = other[column]
                    break

        category_ids = sorted({category for row in members for category in row['_category_ids']})
        merged['frequency_band'] = self.frequency_of(categories=category_ids)
        merged['cefr'] = self.level_of(categories=category_ids)
        merged['categories'] = ' '.join(
            category for category in category_ids
            if category not in FREQUENCY_CATEGORIES and CEFR_CATEGORY_PATTERN.search(category) is None
        )
        merged['status'] = 'studied' if any(row['status'] == 'studied' for row in members) else 'new'
        return merged

    @staticmethod
    def richness(*, row: dict, target_examples_column: str) -> int:
        fields = (row.get('article', ''), row.get('part_of_speech', ''), row.get('ipa', ''), row.get(target_examples_column, ''))
        return sum(1 for value in fields if value)

    @staticmethod
    def sense_set(*, translations: str) -> typing.Set[str]:
        return {sense.strip().lower() for sense in translations.split(TRANSLATION_SEPARATOR) if sense.strip()}

    @staticmethod
    def build_key(*, article: str, word: str, translations_native: str) -> str:
        headword = f'{article} {word}'.strip()
        first_sense = translations_native.split(TRANSLATION_SEPARATOR, 1)[0].strip()
        return f'{headword} ({first_sense})' if first_sense else headword

    @staticmethod
    def assert_unique_keys(*, rows: typing.List[dict]) -> None:
        seen: typing.Set[str] = set()
        for row in rows:
            assert row['key'] not in seen, f"duplicate key: {row['key']!r}"
            seen.add(row['key'])

    @staticmethod
    def decode_pos(*, pos: typing.Optional[int]) -> typing.Dict[str, str]:
        if pos is None:
            return {'part_of_speech': '', 'gender': '', 'number': ''}
        return {
            'part_of_speech': ' '.join(name for bit, name in POS_BITS.items() if pos & (1 << bit)),
            'gender': ' '.join(name for bit, name in GENDER_BITS.items() if pos & (1 << bit)),
            'number': 'pl' if pos & (1 << PLURAL_BIT) else '',
        }

    @staticmethod
    def split_article(*, word: str, particles: typing.Sequence[str]) -> typing.Tuple[str, str]:
        pattern = leading_particle_pattern(tuple(particles))
        match = pattern.match(word) if pattern is not None else None
        if match is None:
            return '', word
        return match.group(1).lower(), word[match.end():]

    @staticmethod
    def split_principal_parts(*, word: str) -> typing.Tuple[str, typing.Dict[str, typing.List[str]]]:
        """An irregular verb arrives as its three forms on one line; only the first is the headword.

        Left whole, the card asks the learner to recall a dash-separated list, and no dictionary
        or image search answers to it either. The other forms are worth keeping, so they move to
        the field that states a word's inflection.
        """
        parts = [part.strip() for part in word.split(PRINCIPAL_PARTS_SEPARATOR)]
        if len(parts) != len(PRINCIPAL_PART_LABELS) + 1 or not all(parts):
            return word, {}
        if any(' ' in part for part in parts):
            return word, {}
        return parts[0], {label: [part] for label, part in zip(PRINCIPAL_PART_LABELS, parts[1:])}

    @staticmethod
    def extract_ipa(*, transcription: typing.Optional[str]) -> str:
        if not transcription:
            return ''
        groups = TRANSCRIPTION_PATTERN.findall(transcription)
        return groups[-1] if groups else ''

    @staticmethod
    def level_of(*, categories: typing.Iterable[str]) -> str:
        """The earliest level the word is listed at, which is where a learner first meets it."""
        found = {
            match.group(1).upper()
            for match in (CEFR_CATEGORY_PATTERN.search(category) for category in categories)
            if match is not None
        }
        return next((level for level in CEFR_LEVELS_LOW_TO_HIGH if level in found), '')

    @staticmethod
    def frequency_of(*, categories: typing.Iterable[str]) -> str:
        category_set = set(categories)
        for frequency in FREQUENCY_CATEGORIES:
            if frequency in category_set:
                return frequency
        return ''

    @staticmethod
    def clean(value: typing.Optional[str]) -> str:
        """TSV cells must stay single-line, so collapse every run of whitespace."""
        if value is None:
            return ''
        return ' '.join(value.split())

    @staticmethod
    def join_lines(values: typing.Iterable[str]) -> str:
        joined = '<br>'.join(values)
        return '' if not joined.strip('<br>') else joined

    @staticmethod
    def align_examples(
        *,
        native: typing.List[typing.Tuple[str, str]],
        pivot: typing.List[typing.Tuple[str, str]],
    ) -> typing.Tuple[typing.List[str], typing.List[str], typing.List[str]]:
        """The native and pivot sets carry overlapping but different target-language sentences.

        Keep their union and pad both translation lists so the Nth line of every
        example column always describes the same sentence.
        """
        by_native = {original: translation for original, translation in native if original}
        by_pivot = {original: translation for original, translation in pivot if original}
        originals: typing.List[str] = []
        for original, _ in native + pivot:
            if original and original not in originals:
                originals.append(original)
        return (
            originals,
            [by_native.get(original, '') for original in originals],
            [by_pivot.get(original, '') for original in originals],
        )

    @classmethod
    def parse_examples(cls, *, raw: typing.Optional[str]) -> typing.List[typing.Tuple[str, str]]:
        if not raw:
            return []
        with contextlib.suppress(json.JSONDecodeError):
            return [
                (cls.strip_marks(cls.clean(example.get('o'))), cls.strip_marks(cls.clean(example.get('t'))))
                for example in json.loads(raw)
            ]
        return []

    @staticmethod
    def strip_marks(text: str) -> str:
        return TARGET_WORD_PATTERN.sub(r'\1', text)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('backup', type=pathlib.Path, help="Path to the ReWord .backup SQLite file")
    parser.add_argument(
        'output', nargs='?', type=pathlib.Path, default=None,
        help="Override the default data/<target_suffix>/words.tsv output path",
    )
    parser.add_argument('--categories', nargs='+', default=list(DEFAULT_CATEGORIES), help="Category ids to export")
    parser.add_argument('--no-studied', action='store_true', help="Skip words already in progress")
    language_config.add_language_arguments(parser)
    arguments = parser.parse_args()

    root = language_config.PROJECT_ROOT
    language = language_config.language_from(arguments, root=root)
    output_path = arguments.output or root / 'data' / language.target / 'words.tsv'
    output_path.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(f'file:{arguments.backup}?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    exporter = WordExporter(
        connection=connection,
        language=language,
        fixes=SourceFixes.load(code=language.target, root=root),
    )
    word_ids = exporter.select_word_ids(
        categories=arguments.categories,
        include_studied=not arguments.no_studied,
    )
    rows, merged_count = exporter.rows(word_ids=word_ids)

    with output_path.open('w', encoding='utf-8') as stream:
        stream.write('\t'.join(language.columns) + '\n')
        for row in rows:
            stream.write('\t'.join(row.get(column, '') for column in language.columns) + '\n')

    print(f"merged {merged_count} duplicate rows")
    print(f"exported {len(rows)} words to {output_path}")


if __name__ == '__main__':
    main()
