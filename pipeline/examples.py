"""Example sentences as parallel lines, shared by every stage that reads or writes them.

The example columns are index-aligned: the Nth line of each describes one sentence, and a
sentence with no translation into some language holds an empty line rather than shifting
the rest. Splitting those columns by hand is exactly how the alignment gets broken, so
every stage goes through this module instead.
"""

import typing

from . import language_config

LINE_SEPARATOR = '<br>'
# A card shows only its best sentences; the rest stay in the fields for the browser.
CARD_EXAMPLE_LIMIT = 3
# Sentences a beginner can read in one breath. Anything outside this is ranked lower.
SHORTEST_USEFUL_WORD_COUNT = 4
LONGEST_USEFUL_WORD_COUNT = 8


class Example(typing.NamedTuple):
    """One sentence and whatever translations of it exist, keyed by language suffix."""

    target: str
    translations: typing.Dict[str, str]

    def missing(self, *, suffixes: typing.Sequence[str]) -> typing.List[str]:
        return [suffix for suffix in suffixes if not self.translations.get(suffix, '').strip()]

    @property
    def _length_penalty(self) -> int:
        word_count = len(self.target.split())
        if word_count < SHORTEST_USEFUL_WORD_COUNT:
            return SHORTEST_USEFUL_WORD_COUNT - word_count
        if word_count > LONGEST_USEFUL_WORD_COUNT:
            return word_count - LONGEST_USEFUL_WORD_COUNT
        return 0


class ExampleSet:
    """The example columns of one row, read and written as a unit."""

    @staticmethod
    def translation_suffixes(*, language: language_config.LanguageConfig) -> typing.List[str]:
        suffixes = [language.native]
        if language.pivot is not None:
            suffixes.append(language.pivot)
        return suffixes

    @classmethod
    def read(cls, *, row: dict, language: language_config.LanguageConfig) -> typing.List[Example]:
        suffixes = cls.translation_suffixes(language=language)
        columns = {
            suffix: row.get(language.examples_column(suffix=suffix), '').split(LINE_SEPARATOR)
            for suffix in suffixes
        }
        targets = row.get(language.examples_column(suffix=language.target), '').split(LINE_SEPARATOR)
        examples = []
        for index, target in enumerate(targets):
            if not target.strip():
                continue
            examples.append(Example(
                target=target.strip(),
                translations={
                    suffix: cls._line(lines=lines, index=index)
                    for suffix, lines in columns.items()
                },
            ))
        return examples

    @classmethod
    def write(
        cls,
        *,
        row: dict,
        examples: typing.Sequence[Example],
        language: language_config.LanguageConfig,
    ) -> dict:
        """Return the row with its example columns replaced, still index-aligned."""
        updated = dict(row)
        updated[language.examples_column(suffix=language.target)] = LINE_SEPARATOR.join(
            example.target for example in examples
        )
        for suffix in cls.translation_suffixes(language=language):
            updated[language.examples_column(suffix=suffix)] = LINE_SEPARATOR.join(
                example.translations.get(suffix, '') for example in examples
            )
        return updated

    @classmethod
    def rank(cls, *, examples: typing.Sequence[Example], suffixes: typing.Sequence[str]) -> typing.List[Example]:
        """Best first: fully translated before half translated, then readable lengths.

        Ranking rather than filtering keeps every sentence, so a later stage can still
        translate the ones that fell below the card's limit.
        """
        return sorted(
            examples,
            key=lambda example: (len(example.missing(suffixes=suffixes)), example._length_penalty),
        )

    @staticmethod
    def locate(*, examples: typing.Sequence[Example], target: str) -> typing.Optional[int]:
        """Find a sentence by its text, for an answer that named it by line number.

        A line number means something only while the lines hold still, and they do not: ranking
        keys on how much a sentence is missing, so filling one gap re-sorts the row the next time
        it is ranked. Matching on the text lets an answer survive that move, and makes an answer
        about a sentence that is no longer there fail to apply rather than land on its neighbour.
        A row repeating one sentence resolves to the first; the copies are interchangeable.
        """
        wanted = target.strip()
        for index, example in enumerate(examples):
            if example.target == wanted:
                return index
        return None

    @staticmethod
    def _line(*, lines: typing.Sequence[str], index: int) -> str:
        return lines[index].strip() if index < len(lines) else ''
