"""Take off a card the related words that belong to another meaning of its headword.

The two halves of a card are built by different steps. The dictionary pass picks a sense — the
first one under the right part of speech — and the model then writes the card's definition from
the supplied translation. Where the two disagree the model follows the translation, which is the
right call and hides the problem: the card reads correctly while the dictionary sense recorded
beside it describes something else.

That stayed harmless only while nothing else read that sense. It stopped being harmless once a
card's synonyms began coming from it, because a sense pointing at the wrong meaning hands over the
wrong meaning's synonyms. A card teaching the palm of a hand offered `palm tree`; one teaching the
rodent offered `fink` and `betrayer`; one teaching a temple offered `template`.

This takes over the half of the synonym review that used to be asked of a model — deciding which
listed synonyms are not synonyms at all. Glossing them in the learner's language still needs one.

## Judging a synonym without deciding what the card means

The obvious repair is to work out which sense the card teaches and keep only that sense's words.
It does not survive contact with the data. Comparing a card's definition with a sense is a
comparison of two sentences written by different hands, and no single way of scoring it holds up:
count the words they share and the true meaning of `elbow` loses to a bend in a wall, weigh how
tightly they fit instead and the true meaning of `palm` loses to the flat face of an anchor. The
two orderings disagree on real cards, in both directions.

The question does not have to be that hard. A synonym does not need the card's sense identified —
it needs only to be shown to belong to a *different* one. So each synonym is judged against the
sense the entry states it under, and only the unarguable case is acted on: where that sense and
the card have not one word in common, the synonym is about another meaning and comes off.

Insisting on none rather than fewest is what makes the pass trustworthy. A rodent and a person
known for betrayal share nothing to miss, so dropping `fink` from the rat is safe; a fingernail
worded two ways shares one word, which is thin evidence of agreement but no evidence at all of
disagreement, so `onychium` stays. Roughly one card in eight is touched, and the rest are left no
worse than they were.

## What stays

A synonym the entry does not mention anywhere was written by the model against the card's own
meaning, and stays — the earlier pass asked for one only where the dictionary had none, so these
are the cards' only synonyms. Removing them because the dictionary is silent would punish exactly
the words nothing is wrong with.

A synonym whose sense does overlap the card stays too, and the distinction is finer than it looks:
for the palm of a hand this drops `palm tree` while keeping `loof`, which the entry states under
the very sense the card teaches.
"""

import argparse
import collections
import itertools
import json
import pathlib
import re
import typing

from . import dictionaries
from . import language_config

CARDS_FILENAME = 'cards.tsv'
MERGED_COLUMN = 'merged_into'
SOURCE_COLUMN = 'relations_source'
SOURCE_NAME = 'sense pruning'
RELATED_COLUMNS = ('synonyms', 'antonyms')
LIST_SEPARATOR = ', '

# How many content words a sense and the card's definition must share before they are taken to be
# about the same thing. One shared word is coincidence: it read `pleasure` as sexual enjoyment on
# the strength of the word "enjoyment" alone.
MINIMUM_SHARED_WORDS = 2

WORD_PATTERN = re.compile(r'[a-z]+')
# Plural and participle endings, so a card's `bones` matches a sense's `bone`.
SUFFIX_PATTERN = re.compile(r'(ing|ed|es|s)$')
MINIMUM_WORD_LENGTH = 3

# Words every definition reaches for, which say nothing about which meaning is being defined.
FILLER_WORDS = frozenset("""
a an the of to in on for and or with that which is are be been being
any some someone something person thing used use as at by from into other such
""".split())


class SensePruning:
    """Deciding which of a card's related words belong to a meaning the card does not teach."""

    def __init__(self, *, language: language_config.LanguageConfig):
        self._language = language

    @staticmethod
    def content_words(*, text: str) -> typing.Set[str]:
        return {
            SUFFIX_PATTERN.sub('', word) for word in WORD_PATTERN.findall(text.lower())
            if word not in FILLER_WORDS and len(word) >= MINIMUM_WORD_LENGTH
        }

    @staticmethod
    def senses_stating(
        *, definitions: typing.Sequence[dictionaries.WiktionaryDefinition],
    ) -> typing.Dict[str, typing.List[str]]:
        """Which senses each related word in the entry is stated under, keyed by the word."""
        stated: typing.Dict[str, typing.List[str]] = collections.defaultdict(list)
        for definition in definitions:
            for word in (*definition.synonyms, *definition.antonyms):
                stated[word.strip().lower()].append(definition.text)
        return stated

    def _closeness(self, *, card_words: typing.Set[str], sense: str) -> int:
        return len(card_words & self.content_words(text=sense))

    def belongs_elsewhere(
        self,
        *,
        word: str,
        card_words: typing.Set[str],
        stated: typing.Dict[str, typing.List[str]],
    ) -> bool:
        """Whether every sense the entry states this word under has nothing at all to do with the card.

        Nothing at all, not merely less than some other sense. Comparing a card's plain definition
        with a dictionary's technical one is too blunt to rank two plausible senses: a fingernail is
        "the hard flat part at the end of a finger" on the card and "the horny plate or appendage at
        the ends of the fingers" in the entry, sharing a single word, while an unrelated sense of
        the same entry can share three by accident. Every ranking tried on this got real cards
        wrong, in both directions.

        No shared word at all is a different kind of evidence, and the only kind worth acting on
        here. A rodent and a person known for betrayal have nothing in common to miss.
        """
        senses = stated.get(word.lower())
        if not senses:
            return False
        return max(self._closeness(card_words=card_words, sense=sense) for sense in senses) == 0

    def _closest_in_entry(
        self, *, card_words: typing.Set[str],
        definitions: typing.Sequence[dictionaries.WiktionaryDefinition],
    ) -> int:
        """How near the entry's best-fitting sense comes to what the card describes.

        Nothing can be judged until this clears the minimum. A card's plain definition and a
        dictionary's technical one routinely name the same creature in no shared words — a deer is
        "an animal with long thin legs" on the card and "a ruminant mammal of the family Cervidae"
        in the entry — and every sense then looks equally foreign, the right one included. Acting
        there strips a card of synonyms that were never wrong.
        """
        return max(
            (self._closeness(card_words=card_words, sense=definition.text)
             for definition in definitions),
            default=0,
        )

    def prune(
        self, *, row: dict, parsed: dictionaries.WiktionaryResult,
    ) -> typing.Optional[dict]:
        """The row with its foreign-sense related words removed, or nothing when none were."""
        # An edition that files related words under a block rather than a sense never said which
        # meaning owns them, so there is nothing here to judge them against.
        if not parsed.related_words_are_per_sense:
            return None
        # A card whose related words were already taken from its own sense has nothing to prune,
        # and judging them by word overlap only strips good ones.
        if row.get(SOURCE_COLUMN, '').strip() not in ('', SOURCE_NAME):
            return None
        card_words = self.content_words(text=row.get(self._language.definition_column, ''))
        if not card_words:
            return None
        if self._closest_in_entry(card_words=card_words, definitions=parsed.definitions) < MINIMUM_SHARED_WORDS:
            return None

        stated = self.senses_stating(definitions=parsed.definitions)
        updated = dict(row)
        removed = []
        for column in RELATED_COLUMNS:
            gloss_column = f'{column}_{self._language.native}'
            listed = [part.strip() for part in row.get(column, '').split(LIST_SEPARATOR)]
            glosses = [part.strip() for part in row.get(gloss_column, '').split(LIST_SEPARATOR)]
            # The gloss column is read position by position against the list it explains, so a
            # word and its gloss have to leave together or every gloss after the gap shifts onto
            # the wrong word. Dropping one word from `palm` once left `loof` glossed as a tree.
            kept = [
                (word, gloss) for word, gloss in itertools.zip_longest(listed, glosses, fillvalue='')
                if word and not self.belongs_elsewhere(word=word, card_words=card_words, stated=stated)
            ]
            if len(kept) != len([word for word in listed if word]):
                removed.extend(word for word in listed if word and word not in dict(kept))
                updated[column] = LIST_SEPARATOR.join(word for word, _ in kept)
                updated[gloss_column] = LIST_SEPARATOR.join(gloss for _, gloss in kept)
        if not removed:
            return None
        updated[SOURCE_COLUMN] = SOURCE_NAME
        return updated


class CachedEntries:
    """The dictionary entries the earlier pass already fetched, read again without the network."""

    def __init__(self, *, directory: pathlib.Path, language: language_config.LanguageConfig):
        self._directory = directory
        self._language = language

    def parsed(self, *, word: str) -> typing.Optional[dictionaries.WiktionaryResult]:
        lemma = dictionaries.RowEnricher.lookup_lemma(word=word)
        path = self._directory / f'{lemma}.json'
        if not path.exists():
            return None
        entry = json.loads(path.read_text(encoding='utf-8'))
        if not entry.get('found'):
            return None
        return dictionaries.WiktionaryClient._parse(
            wikitext=entry['wikitext'], language=self._language,
        )


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
    if limit is not None:
        rows = rows[:limit]

    pruning = SensePruning(language=language)
    entries = CachedEntries(
        directory=data_directory / 'cache' / language.dictionary, language=language,
    )

    counts: collections.Counter = collections.Counter()
    shown = []
    pruned_rows = []
    for row in rows:
        parsed = None if row.get(MERGED_COLUMN, '').strip() else entries.parsed(word=row['word'])
        pruned = pruning.prune(row=row, parsed=parsed) if parsed is not None else None
        if pruned is None:
            counts['unchanged'] += 1
            pruned_rows.append(row)
            continue
        counts['pruned'] += 1
        if len(shown) < 20:
            shown.append((row['word'], row.get('synonyms', ''), pruned.get('synonyms', '')))
        pruned_rows.append(pruned)

    print(f'{counts["pruned"]} cards lost a related word belonging to another meaning; '
          f'{counts["unchanged"]} unchanged')
    for word, before, after in shown:
        print(f'  {word}: {before or "(none)"} -> {after or "(none)"}')
    if dry_run:
        print('nothing written')
        return
    language_config.TsvFile.write(cards_path, rows=pruned_rows, columns=language.card_columns)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true', help="Report what would change, change nothing")
    parser.add_argument('--limit', type=int, default=None, help="Process only the first N rows")
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
