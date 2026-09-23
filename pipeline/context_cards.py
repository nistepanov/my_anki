"""Pick, for each word, which of its example sentences is worth a card of its own.

A sentence card works when the target word is the one thing in the sentence the learner does not
already know. A sentence carrying two or three other unknown words stops testing the target and
becomes a reading exercise, so the sentence has to be chosen for the job rather than merely be
available. The candidates already sit on the card from the examples stage; this stage writes no
new sentence, it only decides which one, if any, earns a card.

## The learner's vocabulary is knowable, so nothing here is judged

"Does the learner know this word" was asked of a model, one word at a time, which was both slow
and unnecessary: the answer is a set the deck can compute. Whatever is common enough in the
language, plus every word the deck has already taught, is what a learner at this point knows. A
sentence qualifies when every word in it except the target is in that set.

Measuring it rather than judging it also finds more sentences than the judgement did, because a
model asked to be careful about difficulty errs toward saying no.

## Choosing among the sentences that qualify

Several usually qualify, and the shortest is the wrong pick. "Is that a lynx?" says nothing about
what a lynx is; "You have the eyes of a lynx" does. So sentences are ranked by how much they
actually tell the reader — how many words stand beside the target that are not grammar — and only
then by sitting near a comfortable reading length.

A sentence with nothing beside the target but function words is dropped rather than ranked last.
It cannot teach the word, and a card that shows it teaches the learner to guess.
"""

import argparse
import collections
import pathlib
import re
import typing

import wordfreq

from . import examples
from . import language_config

CARDS_FILENAME = 'cards.tsv'
INDEX_COLUMN = 'context_index'
TARGET_COLUMN = 'context_target'
MERGED_COLUMN = 'merged_into'

# How far down the frequency list a learner at this deck's level is assumed to know. Wide enough
# that ordinary sentences are not rejected over a common word the deck never happened to teach.
KNOWN_WORD_COUNT = 5000
# A sentence has to say at least this much about the target beyond grammar, or it teaches nothing.
MINIMUM_CLUE_WORDS = 2
# Long enough to carry a clue, short enough to read on a phone without effort; sentences are
# ranked by nearness to this rather than filtered on it.
COMFORTABLE_SENTENCE_LENGTH = 8

WORD_PATTERN = re.compile(r"[^\W\d_][\w'-]*", re.UNICODE)

# Endings a learner reads straight through. Stripping them lets a sentence's `carries` match a
# known `carry`, and a target's `kidneys` match the headword `kidney`.
INFLECTION_ENDINGS = (
    ("n't", ''), ("'s", ''), ('ies', 'y'), ('es', ''), ('ed', ''), ('ing', ''),
    ('est', ''), ('er', ''), ('ly', ''), ('s', ''), ('d', ''),
)

# Grammar the sentence needs but which says nothing about the target word.
FUNCTION_WORDS = frozenset("""
a an the this that these those here there
is are was were be been being am do does did done have has had having
will would shall should can could may might must
i you he she it we they me him her us them my your his its our their
in on at to of for from by with as into onto about over under after before
and or but not no nor so if then than too very just now also only
one two some any all both each other another same such what which who whom whose
""".split())


class SentenceChoice(typing.NamedTuple):
    """One example sentence judged fit to carry a card, and why it ranked where it did."""

    index: int
    target: str
    clue_words: int
    length: int
    text: str

    def rank(self) -> typing.Tuple[int, int]:
        """Best first: most telling, then nearest a comfortable reading length."""
        return self.clue_words, -abs(self.length - COMFORTABLE_SENTENCE_LENGTH)


class KnownVocabulary:
    """The words a learner of this deck is taken to already know.

    Two sources, because neither alone is the answer. A frequency list holds the everyday words no
    course bothers to teach and the deck therefore never contains; the deck holds the words this
    learner was actually taught, including uncommon ones a frequency list places far down.
    """

    def __init__(self, *, forms: typing.Set[str]):
        self._forms = forms

    def __contains__(self, word: str) -> bool:
        form = word.lower()
        return form in self._forms or bool(self.stems(word=form) & self._forms)

    @staticmethod
    def stems(*, word: str) -> typing.Set[str]:
        """A word and the forms it might be an inflection of, since neither source is inflected.

        Deliberately generous and occasionally wrong: treating an unknown word as known costs one
        slightly hard sentence, while missing an inflection costs a good sentence entirely.
        """
        found = {word}
        for ending, replacement in INFLECTION_ENDINGS:
            if word.endswith(ending) and len(word) > len(ending) + 1:
                stem = word[:-len(ending)] + replacement
                found.update({stem, f'{stem}e'})
        if len(word) > 4 and word[-1] == word[-2]:
            found.add(word[:-1])
        return found

    @classmethod
    def build(
        cls, *, rows: typing.Sequence[dict], language: language_config.LanguageConfig,
    ) -> 'KnownVocabulary':
        taught = {
            row['word'].strip().lower() for row in rows
            if row.get('cefr', '').strip().upper() in language.mastered_levels
        }
        common = set(wordfreq.top_n_list(language.target, KNOWN_WORD_COUNT))
        forms = set(taught) | common
        for word in list(forms):
            forms.update(cls.stems(word=word))
        return cls(forms=forms)


class ContextPlan:
    """Whether one of a word's example sentences is worth a card of its own, and which."""

    def __init__(self, *, language: language_config.LanguageConfig, known: KnownVocabulary):
        self._language = language
        self._known = known

    @staticmethod
    def matches_filters(
        *, row: dict, levels: typing.Optional[typing.Set[str]], bands: typing.Optional[typing.Set[str]],
    ) -> bool:
        """Whether a row belongs to the level/band subset a caller asked to narrow to.

        A row missing the column, or carrying a value the caller did not list, is out of scope —
        there is nothing to tell it apart from a value nobody asked for, so it is skipped either way.
        """
        if levels is not None and row.get('cefr', '').strip().lower() not in levels:
            return False
        if bands is not None and row.get('frequency_band', '').strip().lower() not in bands:
            return False
        # A row folded into another card is not a card, so choosing a sentence for it is work
        # nothing will ever show.
        return not row.get(MERGED_COLUMN, '').strip()

    def _target_forms(self, *, word: str) -> typing.Set[str]:
        headword = word.split(',', 1)[0].strip().lower()
        forms = {headword}
        for part in headword.split():
            forms.add(part)
        return {form for term in set(forms) for form in KnownVocabulary.stems(word=term)} | forms

    def _judge(self, *, sentence: str, index: int, target_forms: typing.Set[str]) -> typing.Optional[SentenceChoice]:
        """The sentence as a candidate, or nothing when it cannot carry a card."""
        words = WORD_PATTERN.findall(sentence)
        if not words:
            return None

        appearance = next((word for word in words if word.lower() in target_forms), None)
        if appearance is None:
            return None

        clue_words = 0
        for word in words:
            form = word.lower()
            if form in target_forms:
                continue
            if form not in self._known:
                return None
            if form not in FUNCTION_WORDS:
                clue_words += 1
        if clue_words < MINIMUM_CLUE_WORDS:
            return None

        return SentenceChoice(
            index=index, target=appearance, clue_words=clue_words, length=len(words), text=sentence,
        )

    def choose(self, *, row: dict) -> typing.Optional[SentenceChoice]:
        """The best sentence this row can offer, or nothing when none qualifies."""
        target_forms = self._target_forms(word=row['word'])
        candidates = [
            choice for index, example in enumerate(
                examples.ExampleSet.read(row=row, language=self._language), start=1,
            )
            for choice in [self._judge(sentence=example.target, index=index, target_forms=target_forms)]
            if choice is not None
        ]
        return max(candidates, key=SentenceChoice.rank) if candidates else None

    def apply(
        self, *, rows: typing.List[dict], only_missing: bool = False,
    ) -> typing.Tuple[typing.List[dict], collections.Counter]:
        """Every row with its chosen sentence recorded, and a count of what happened."""
        counts: collections.Counter = collections.Counter()
        updated_rows = []
        for row in rows:
            if only_missing and row.get(TARGET_COLUMN, '').strip():
                counts['kept'] += 1
                updated_rows.append(row)
                continue
            choice = self.choose(row=row)
            updated = dict(row)
            if choice is None:
                # Cleared rather than left alone: a row whose sentences no longer qualify must stop
                # pointing at one, or the card shows a sentence this stage would now reject.
                counts['none'] += 1
                updated[INDEX_COLUMN] = ''
                updated[TARGET_COLUMN] = ''
            else:
                counts['chosen'] += 1
                updated[INDEX_COLUMN] = str(choice.index)
                updated[TARGET_COLUMN] = choice.target
            updated_rows.append(updated)
        return updated_rows, counts


def main_for(
    *,
    language: language_config.LanguageConfig,
    root: pathlib.Path,
    dry_run: bool = False,
    limit: typing.Optional[int] = None,
    levels: typing.Optional[typing.Set[str]] = None,
    bands: typing.Optional[typing.Set[str]] = None,
    only_missing: bool = False,
) -> None:
    data_directory = language.data_directory(root=root)
    cards_path = data_directory / CARDS_FILENAME
    rows = language_config.TsvFile.read(cards_path)
    if limit is not None:
        rows = rows[:limit]

    # Built from every row, including the ones this run will not touch: what the learner knows does
    # not depend on which subset is being replanned.
    known = KnownVocabulary.build(rows=rows, language=language)
    plan = ContextPlan(language=language, known=known)

    in_scope = [row for row in rows if plan.matches_filters(row=row, levels=levels, bands=bands)]
    chosen_rows, counts = plan.apply(rows=in_scope, only_missing=only_missing)
    by_key = {row['key']: row for row in chosen_rows}
    merged = [by_key.get(row['key'], row) for row in rows]

    considered = len(in_scope)
    print(f'{counts["chosen"]} of {considered} words have a sentence worth a card '
          f'({counts["none"]} have none' + (f', {counts["kept"]} already chosen' if counts['kept'] else '') + ')')
    if dry_run:
        for row in chosen_rows[:20]:
            if row.get(TARGET_COLUMN, '').strip():
                print(f'  {row["word"]}: {row[TARGET_COLUMN]}')
        print('nothing written')
        return
    language_config.TsvFile.write(cards_path, rows=merged, columns=language.card_columns)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true', help="Report what would be chosen, change nothing")
    parser.add_argument('--limit', type=int, default=None, help="Process only the first N rows")
    parser.add_argument(
        '--level', default=None,
        help="Comma-separated CEFR levels to choose for, e.g. b1 or a2,b1; absent means every level",
    )
    parser.add_argument(
        '--band', default=None,
        help="Comma-separated frequency bands to choose for, e.g. top100,top1000; absent means every band",
    )
    parser.add_argument(
        '--only-missing', action='store_true',
        help="Leave words that already carry a chosen sentence alone",
    )
    language_config.add_language_arguments(parser)
    arguments = parser.parse_args()

    root = language_config.PROJECT_ROOT
    main_for(
        language=language_config.language_from(arguments, root=root),
        root=root,
        dry_run=arguments.dry_run,
        limit=arguments.limit,
        levels={level.strip().lower() for level in arguments.level.split(',') if level.strip()} if arguments.level else None,
        bands={band.strip().lower() for band in arguments.band.split(',') if band.strip()} if arguments.band else None,
        only_missing=arguments.only_missing,
    )


if __name__ == '__main__':
    main()
