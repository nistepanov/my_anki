"""Fold together the cards that teach one word twice.

A headword gets a card per sense, which is right for a spelling carrying unrelated meanings —
`pupil` the eye and `pupil` the schoolchild deserve their own cards and their own review history.
It is wrong for a word whose senses are one meaning wearing two grammatical hats. English turns a
verb into a noun without changing a letter, so `escape` arrives twice: once as getting away and
once as the act of getting away. The learner meets one word, is asked about it twice, and the
second asking teaches nothing the first did not.

The same fold catches two outright mistakes the sense-splitting produced: a word split on a
distinction that is not one — `hostage` filed separately as the man and the woman — and a word
split into two cards carrying the same definition word for word.

## Deciding that two cards are one

Two cards of a headword describe one meaning when their definitions do, once the wrapper that
only states a part of speech is taken off. "To get away from a dangerous place" and "The act of
getting away from a dangerous place" are the same sentence with a noun's clothing on, and
stripping the opener plus the participle ending leaves two strings that barely differ.

Comparing what is left is a measurement rather than a judgement, which is the point: nothing here
is asked of a model. It is not free of error either, so the threshold is set where a pair has to
look obviously alike, and nothing is thrown away.

## Nothing is deleted

The folded-away row keeps its place in the table and records which card it was folded into. The
deck skips it, and a bad fold can be found by reading the column rather than by reconstructing
what was lost. This also makes the fold something a re-run can see has already happened.

The note it left behind in Anki is a different matter: Anki holds a copy the table cannot reach,
and leaving it there means the deck keeps asking a question the table no longer has. It has to be
retired deliberately, which costs that note's review history — so it is a separate, named step
rather than a silent part of a push.
"""

import argparse
import collections
import difflib
import pathlib
import re
import typing

from . import examples
from . import frequency
from . import language_config

CARDS_FILENAME = 'cards.tsv'
MERGED_COLUMN = 'merged_into'

# Two definitions have to look this alike, once stripped, before one card is folded into the
# other. Set where a pair is obviously the same sentence in two grammatical shapes; lower, and
# words merely close in meaning start folding together.
SIMILARITY_THRESHOLD = 0.60

# The openers a definition wears only to announce its part of speech. Stripped before comparing,
# because they are exactly what differs between a verb's definition and its noun's.
PART_OF_SPEECH_OPENER_PATTERN = re.compile(
    r'^\s*(this\s+word\s+(?:means|describes|refers\s+to|is)\s+|'
    r'to\s+|the\s+act\s+of\s+|an?\s+act\s+of\s+|the\s+state\s+of\s+|the\s+fact\s+of\s+|'
    r'the\s+process\s+of\s+|the\s+quality\s+of\s+|a\s+|an\s+|the\s+)', re.IGNORECASE,
)
# A noun's definition reaches for the participle where a verb's uses the bare stem: "getting away"
# against "get away". Cutting the ending lets the two words match.
PARTICIPLE_PATTERN = re.compile(r'\b(\w+?)ing\b')
NON_LETTER_PATTERN = re.compile(r'[^a-z ]+')

# Parts of speech keep this order when two cards' are combined, so "verb noun" and "noun verb"
# cannot both appear for the same kind of pair.
PART_OF_SPEECH_ORDER = (
    'noun', 'verb', 'adjective', 'adverb', 'preposition', 'conjunction', 'pronoun', 'participle',
)
LIST_SEPARATOR = ', '
TAG_SEPARATOR = ' '
STUDIED_STATUS = 'studied'


class DuplicatePair(typing.NamedTuple):
    """Two cards of one headword that teach the same meaning, and how alike they read."""

    similarity: float
    keeper: dict
    folded: dict


class Duplicates:
    """Finding the cards of a headword that teach one meaning, and folding them into one."""

    def __init__(self, *, language: language_config.LanguageConfig):
        self._language = language
        self._native_bands = frequency.FrequencyBands(language_code=language.native)

    @staticmethod
    def comparable(*, definition: str) -> str:
        """A definition reduced to what it says, with the part-of-speech clothing taken off."""
        text = definition.strip().lower()
        previous = None
        while previous != text:
            previous = text
            text = PART_OF_SPEECH_OPENER_PATTERN.sub('', text).strip()
        text = PARTICIPLE_PATTERN.sub(r'\1', text)
        return ' '.join(NON_LETTER_PATTERN.sub(' ', text).split())

    @classmethod
    def similarity(cls, *, first: str, second: str) -> float:
        return difflib.SequenceMatcher(
            None, cls.comparable(definition=first), cls.comparable(definition=second),
        ).ratio()

    def _keeper_first(self, *, rows: typing.Sequence[dict]) -> typing.List[dict]:
        """The two cards, the one whose note should survive first.

        Only review history decides this. Folding away a card the learner has been studying throws
        that work away; folding away one they have never seen throws away nothing. Which card
        reads better is a separate question, and answering both at once is how the surviving card
        ended up stating that a hostage is a woman.
        """
        def rank(row: dict) -> typing.Tuple[int, int]:
            return (
                1 if row.get('status', '') == STUDIED_STATUS else 0,
                len(examples.ExampleSet.read(row=row, language=self._language)),
            )

        return sorted(rows, key=rank, reverse=True)

    def _better_worded_first(self, *, rows: typing.Sequence[dict]) -> typing.List[dict]:
        """The two cards, the one whose wording the merged card should take first.

        The better definition is the one the other was derived from. A noun built off a verb
        states itself as an act of that verb, so a definition still wearing that opener is the
        derived one. Where neither is derived — two cards of one noun, split on a distinction that
        was not one — the commoner of the two native translations names the word a learner
        actually meets, which is how the general term wins over the narrower one.
        """
        def rank(row: dict) -> typing.Tuple[int, float]:
            definition = row.get(self._language.definition_column, '')
            translations = [
                part for part in row.get(self._language.translation_columns[0], '').split(LIST_SEPARATOR)
                if part.strip()
            ]
            return (
                0 if PART_OF_SPEECH_OPENER_PATTERN.match(definition) else 1,
                self._native_bands.zipf_for(word=translations[0]) if translations else 0.0,
            )

        return sorted(rows, key=rank, reverse=True)

    def find(self, *, rows: typing.List[dict]) -> typing.List[DuplicatePair]:
        """Every pair of cards sharing a headword whose definitions describe one meaning."""
        by_headword = collections.defaultdict(list)
        for row in rows:
            if not row.get(MERGED_COLUMN, '').strip():
                by_headword[row['word'].strip().lower()].append(row)

        definition_column = self._language.definition_column
        found = []
        for candidates in by_headword.values():
            if len(candidates) < 2:
                continue
            for first in range(len(candidates)):
                for second in range(first + 1, len(candidates)):
                    one, other = candidates[first], candidates[second]
                    score = self.similarity(
                        first=one.get(definition_column, ''), second=other.get(definition_column, ''),
                    )
                    if score < SIMILARITY_THRESHOLD:
                        continue
                    keeper, folded = self._keeper_first(rows=(one, other))
                    found.append(DuplicatePair(similarity=score, keeper=keeper, folded=folded))
        return sorted(found, key=lambda pair: pair.similarity, reverse=True)

    @staticmethod
    def _combined(*, first: str, second: str, separator: str) -> str:
        """Both lists as one, in order, with what is already there not repeated."""
        seen = set()
        merged = []
        for part in first.split(separator) + second.split(separator):
            part = part.strip()
            if not part or part.lower() in seen:
                continue
            seen.add(part.lower())
            merged.append(part)
        return separator.join(merged)

    @staticmethod
    def _combined_parts_of_speech(*, first: str, second: str) -> str:
        tokens = {token for token in f'{first} {second}'.split() if token}
        ordered = [name for name in PART_OF_SPEECH_ORDER if name in tokens]
        return TAG_SEPARATOR.join(ordered + sorted(tokens - set(ordered)))

    def merge(self, *, pair: DuplicatePair) -> typing.Tuple[dict, dict]:
        """The kept card with the other's content folded in, and the folded card marked as such.

        The card that survives is the one holding the review history, but the wording it shows is
        whichever of the two reads better — the definition the other was derived from, with its
        translation leading. One sentence stating the meaning is what a card wants; which
        grammatical shape that meaning takes is already on the card as its part of speech.
        """
        keeper, folded = dict(pair.keeper), dict(pair.folded)
        language = self._language
        # Both halves of the card follow the same choice, or a card ends up defining one word and
        # translating another.
        primary, secondary = self._better_worded_first(rows=(keeper, folded))

        # A definition is one sentence, not a list, so it is taken whole rather than combined.
        for column in (language.definition_column, language.definition_full_column,
                       *language.definition_translation_columns):
            keeper[column] = primary.get(column, '')
        keeper['part_of_speech'] = self._combined_parts_of_speech(
            first=primary.get('part_of_speech', ''), second=secondary.get('part_of_speech', ''),
        )
        for column in (*language.translation_columns, 'synonyms', 'antonyms',
                       *language.related_translation_columns):
            keeper[column] = self._combined(
                first=primary.get(column, ''), second=secondary.get(column, ''), separator=LIST_SEPARATOR,
            )
        for column in ('categories', 'tags'):
            keeper[column] = self._combined(
                first=keeper.get(column, ''), second=folded.get(column, ''), separator=TAG_SEPARATOR,
            )
        if folded.get('has_image', '') and not keeper.get('has_image', ''):
            keeper['has_image'] = folded['has_image']
        if not keeper.get('ipa', '').strip():
            keeper['ipa'] = folded.get('ipa', '')

        kept_examples = examples.ExampleSet.read(row=keeper, language=language)
        seen = {example.target.strip().lower() for example in kept_examples}
        for example in examples.ExampleSet.read(row=folded, language=language):
            if example.target.strip().lower() not in seen:
                kept_examples.append(example)
        keeper = examples.ExampleSet.write(row=keeper, examples=kept_examples, language=language)

        # The sentence a context card was built from is addressed by its line number, and the
        # folded card's sentences arrive after the kept card's — so a pointer into the folded
        # card's own numbering would now name a different sentence.
        if not keeper.get('context_index', '').strip():
            keeper['context_index'] = ''
            keeper['context_target'] = ''

        folded[MERGED_COLUMN] = keeper['key']
        return keeper, folded


def main_for(
    *,
    language: language_config.LanguageConfig,
    root: pathlib.Path,
    dry_run: bool = False,
    limit: typing.Optional[int] = None,
) -> None:
    data_directory = language.data_directory(root=root)
    cards_path = data_directory / CARDS_FILENAME
    rows = language_config.TsvFile.read(cards_path)

    duplicates = Duplicates(language=language)
    pairs = duplicates.find(rows=rows)
    if limit is not None:
        pairs = pairs[:limit]
    print(f'{len(pairs)} pairs of cards teach one meaning twice')
    if not pairs:
        return

    by_identity = {id(row): index for index, row in enumerate(rows)}
    for pair in pairs:
        keeper, folded = duplicates.merge(pair=pair)
        print(f'  {pair.similarity:.2f} {keeper["word"]}: '
              f'{pair.folded["key"]!r} folded into {keeper["key"]!r}')
        if dry_run:
            continue
        rows[by_identity[id(pair.keeper)]] = keeper
        rows[by_identity[id(pair.folded)]] = folded

    if dry_run:
        print('nothing written')
        return
    language_config.TsvFile.write(cards_path, rows=rows, columns=language.card_columns)
    print(f'{len(pairs)} cards folded away; run the push to send the merged cards to Anki')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true', help="Report what would merge, change nothing")
    parser.add_argument('--limit', type=int, help="Only act on this many pairs, best match first")
    language_config.add_language_arguments(parser)
    arguments = parser.parse_args()

    root = language_config.PROJECT_ROOT
    main_for(
        language=language_config.language_from(arguments, root=root),
        root=root,
        dry_run=arguments.dry_run,
        limit=arguments.limit,
    )


if __name__ == '__main__':
    main()
