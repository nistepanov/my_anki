"""Import a plain-text wordlist into the same TSV skeleton reword.py produces.

The vocabulary-app backup entry point only exists for languages that have a ReWord
export; everywhere else the source is just a list of words in a text file. Both
entry points feed the same downstream stages, so this one matches reword.py's
key-building and article-splitting behaviour exactly and reuses its static helpers.

Accepted line shapes (fields are delimiter-separated, whitespace-stripped):
    boca
    la boca
    la boca<TAB>рот
    la boca<TAB>рот<TAB>mouth
Blank lines and lines starting with '#' are ignored. Everything columns cannot
derive from the headword alone (part of speech, gender, definition, ...) is left
empty for later enrichment stages to fill.
"""

import argparse
import pathlib
import typing

from . import reword
from . import language_config

DEFAULT_DELIMITER = '\t'
COMMENT_PREFIX = '#'

HEADWORD_INDEX = 0
NATIVE_TRANSLATION_INDEX = 1
PIVOT_TRANSLATION_INDEX = 2
MAX_INPUT_COLUMNS = 3


class ImportSummary(typing.NamedTuple):
    rows: typing.List[typing.Dict[str, str]]
    lines_read: int
    lines_skipped: int
    overflow_count: int
    translated_count: int


class WordlistImporter:
    """Turns a plain wordlist into rows shaped like reword.py's output."""

    def __init__(self, *, language: language_config.LanguageConfig):
        self._language = language

    def import_file(self, *, path: pathlib.Path, delimiter: str) -> ImportSummary:
        lines = path.read_text(encoding='utf-8').splitlines()
        rows: typing.List[typing.Dict[str, str]] = []
        lines_skipped = 0
        overflow_count = 0
        translated_count = 0

        for line_number, line in enumerate(lines, start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith(COMMENT_PREFIX):
                lines_skipped += 1
                continue

            raw_fields = stripped.split(delimiter)
            if len(raw_fields) > MAX_INPUT_COLUMNS:
                overflow_count += 1
            fields = [field.strip() for field in raw_fields[:MAX_INPUT_COLUMNS]]

            if len(fields) > PIVOT_TRANSLATION_INDEX and self._language.pivot is None:
                raise SystemExit(
                    f"line {line_number}: {self._language.code!r} has no pivot language, "
                    f"but a third column was supplied: {stripped!r}"
                )

            row = self.build_row(
                language=self._language,
                headword=fields[HEADWORD_INDEX],
                native_translation=fields[NATIVE_TRANSLATION_INDEX] if len(fields) > NATIVE_TRANSLATION_INDEX else '',
                pivot_translation=fields[PIVOT_TRANSLATION_INDEX] if len(fields) > PIVOT_TRANSLATION_INDEX else '',
            )
            rows.append(row)
            if row[f'translations_{self._language.native}']:
                translated_count += 1

        self.assert_unique_keys(rows=rows)
        return ImportSummary(
            rows=rows,
            lines_read=len(lines),
            lines_skipped=lines_skipped,
            overflow_count=overflow_count,
            translated_count=translated_count,
        )

    @staticmethod
    def build_row(
        *,
        language: language_config.LanguageConfig,
        headword: str,
        native_translation: str,
        pivot_translation: str,
    ) -> typing.Dict[str, str]:
        cleaned_headword = reword.WordExporter.clean(value=headword)
        if language.has_articles:
            article, word = reword.WordExporter.split_article(
                word=cleaned_headword, particles=language.articles,
            )
        else:
            article, word = '', cleaned_headword

        native_translation = reword.WordExporter.clean(value=native_translation)
        pivot_translation = reword.WordExporter.clean(value=pivot_translation)

        row = {column: '' for column in language.extracted_columns}
        row['key'] = reword.WordExporter.build_key(
            article=article, word=word, translations_native=native_translation,
        )
        row['word'] = word
        row['article'] = article
        row[f'translations_{language.native}'] = native_translation
        if language.pivot is not None and pivot_translation:
            row[f'translations_{language.pivot}'] = pivot_translation
        return row

    @staticmethod
    def merge_into(
        *,
        existing: typing.List[typing.Dict[str, str]],
        imported: typing.List[typing.Dict[str, str]],
    ) -> typing.Tuple[typing.List[typing.Dict[str, str]], int]:
        """Append the words not already present, leaving the ones that are untouched.

        The wordlist is a file the learner keeps adding lines to, so re-importing it is the
        normal case, not an accident. A word already in the table has been through the paid
        stages; re-importing it as a bare skeleton would throw that away and pay for it again.
        """
        by_key = {row['key']: row for row in existing}
        added = [row for row in imported if row['key'] not in by_key]
        return existing + added, len(added)

    @staticmethod
    def assert_unique_keys(*, rows: typing.List[typing.Dict[str, str]]) -> None:
        """Lists every offending key, unlike reword.py's version which stops at the first."""
        counts: typing.Dict[str, int] = {}
        for row in rows:
            counts[row['key']] = counts.get(row['key'], 0) + 1
        duplicates = sorted(key for key, count in counts.items() if count > 1)
        assert not duplicates, f"duplicate keys: {duplicates}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('wordlist', type=pathlib.Path, help="Path to the UTF-8 wordlist text file")
    parser.add_argument(
        'output', nargs='?', type=pathlib.Path, default=None,
        help="Override the default data/<target_suffix>/words.tsv output path",
    )
    parser.add_argument(
        '--delimiter', default=DEFAULT_DELIMITER, metavar='TAB',
        help="Field delimiter, e.g. ',' for a CSV wordlist (default: tab)",
    )
    parser.add_argument(
        '--replace', action='store_true',
        help="Discard the existing words.tsv instead of appending the new words to it",
    )
    language_config.add_language_arguments(parser)
    arguments = parser.parse_args()

    root = language_config.PROJECT_ROOT
    language = language_config.language_from(arguments, root=root)
    output_path = arguments.output or language.data_directory(root=root) / 'words.tsv'

    importer = WordlistImporter(language=language)
    summary = importer.import_file(path=arguments.wordlist, delimiter=arguments.delimiter)

    existing = []
    if output_path.exists() and not arguments.replace:
        existing = language_config.TsvFile.read(output_path)
    rows, added = importer.merge_into(existing=existing, imported=summary.rows)

    language_config.TsvFile.write(output_path, rows=rows, columns=language.extracted_columns)

    if summary.overflow_count:
        print(
            f"warning: {summary.overflow_count} line(s) had more than {MAX_INPUT_COLUMNS} columns; "
            "extra columns were discarded"
        )
    print(f"read {summary.lines_read} lines, skipped {summary.lines_skipped} blank or comment")
    print(f"added {added} new words to {len(existing)} already there — {len(rows)} in {output_path}")


if __name__ == '__main__':
    main()
