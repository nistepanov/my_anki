"""Match what the deck asks to the levels the learner has already finished.

A level the learner reports as easy is marked mastered, and its cards are suspended rather than
deleted: the word stays in the deck, keeps its media and its history, and comes back the moment
the level is unmarked. Suspending is what the CEFR split cannot do on its own — the split files
a card, it does not decide whether the card should be asked.

The stage exists because re-levelling keeps moving words across that line, and it moves them
both ways. A word demoted into a finished level is a word the learner already knew, which is
why it moved, but the push files it there still active and it would be served as if it were
new. A word a published list raises out of a finished level has the opposite problem: it was
hidden under a level nobody now claims for it, and while only hiding was done it stayed hidden
for good. Re-levelling a deck that can only hide loses words with every run, so both directions
are settled here.

Only a card that has never been studied is touched. A card carrying review history is a card the
learner is in the middle of, and taking it away mid-schedule loses the schedule; whatever its
level now says, finishing it costs one more review and dropping it costs the work already done.

Bringing a card back is also limited to the kinds of card the deck still builds. A card type the
deck has stopped asking was hidden on purpose and for every word at once, so its level says
nothing about whether it should return.
"""

import argparse
import collections
import pathlib
import typing

from . import anki
from . import language_config

CARDS_FILENAME = 'cards.tsv'

# New in Anki's own scheme; anything else carries a schedule the learner built.
NEW_CARD_TYPE = 0
SUSPENDED_QUEUE = -1
# Anki keeps a card whose front template has come out empty and renders it as an error page linking here.
BLANK_FRONT_MARKER = 'front-of-card-is-blank'


class Mastery:
    """Which of a word's cards the deck means to ask, and which it means to leave alone."""

    def __init__(self, *, client: anki.AnkiConnect, language: language_config.LanguageConfig):
        self._client = client
        self._language = language
        self._ordinals = self._ordinals_by_template()

    def _ordinals_by_template(self) -> typing.Dict[str, int]:
        """A card records the template it was built from as a number rather than a name, so the
        note type's own template order is what a configured name has to be read through."""
        names = self._client.invoke(action='modelTemplates', modelName=self._language.note_type_name)
        return {name.lower(): ordinal for ordinal, name in enumerate(names)}

    def wanted_ordinals(self, *, cards: typing.Sequence[dict]) -> typing.Set[int]:
        """The template slots this word should be asked in, given which cards it actually has.

        A card type the deck has stopped building is unwanted for every word at once. A card
        type that stands down for another is unwanted only where the other was built: a word
        whose sentence was too hard to use has no richer card to be replaced by, and losing the
        plain one would leave it not asked at all.
        """
        wanted = {
            self._ordinals[name] for name in self._language.card_templates
            if name.lower() in self._ordinals
        }
        present = {card['ord'] for card in cards if not self.renders_blank(card=card)}
        for winner, loser in self._language.superseded_cards.items():
            if self._ordinals.get(winner) in wanted & present:
                wanted.discard(self._ordinals.get(loser))
        return wanted

    @staticmethod
    def renders_blank(*, card: dict) -> bool:
        """A card whose field emptied after it was built; it asks nothing, so it is never wanted."""
        return BLANK_FRONT_MARKER in card.get('question', '')

    def cards_by_key(self) -> typing.Dict[str, typing.List[dict]]:
        note_ids = self._client.invoke(action='findNotes', query=f'note:"{self._language.note_type_name}"')
        if not note_ids:
            return {}
        notes = self._client.invoke(action='notesInfo', notes=note_ids)
        card_ids = [card_id for note in notes for card_id in note['cards']]
        cards = {info['cardId']: info for info in self._client.invoke(action='cardsInfo', cards=card_ids)}
        by_key: typing.Dict[str, typing.List[dict]] = {}
        for note in notes:
            key = note['fields'].get('Key', {}).get('value', '')
            if key:
                by_key[key] = [cards[card_id] for card_id in note['cards'] if card_id in cards]
        return by_key


def main_for(
    *,
    language: language_config.LanguageConfig,
    root: pathlib.Path,
    levels: typing.Set[str],
    dry_run: bool = False,
    url: str = anki.ANKI_CONNECT_URL,
) -> None:
    rows = language_config.TsvFile.read(language.data_directory(root=root) / CARDS_FILENAME)
    mastered_keys = {row['key'] for row in rows if row.get('cefr', '').strip().upper() in levels}

    client = anki.AnkiConnect(url=url)
    mastery = Mastery(client=client, language=language)
    cards_by_key = mastery.cards_by_key()

    to_suspend, to_restore, already_hidden, studied = [], [], 0, 0
    for key, cards in cards_by_key.items():
        wanted = mastery.wanted_ordinals(cards=cards)
        asked = key not in mastered_keys
        for card in cards:
            should_ask = asked and card['ord'] in wanted and not Mastery.renders_blank(card=card)
            if card['type'] != NEW_CARD_TYPE:
                studied += 1
            elif card['queue'] != SUSPENDED_QUEUE:
                if not should_ask:
                    to_suspend.append(card['cardId'])
            elif should_ask:
                to_restore.append(card['cardId'])
            else:
                already_hidden += 1

    print(
        f"{len(mastered_keys)} words sit at {', '.join(sorted(levels))} | "
        f"hiding {len(to_suspend)} | bringing back {len(to_restore)} | "
        f"already hidden {already_hidden} | left alone (has review history) {studied}"
    )
    by_level: typing.Counter = collections.Counter(
        row.get('cefr', '') for row in rows if row['key'] in mastered_keys
    )
    for level, count in sorted(by_level.items()):
        print(f"  {level}: {count} words")

    if dry_run:
        print("dry run: changed nothing")
        return
    if to_suspend:
        client.invoke(action='suspend', cards=to_suspend)
    if to_restore:
        client.invoke(action='unsuspend', cards=to_restore)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--levels',
        default=None,
        help="Levels to hide, e.g. A1,A2,B1; absent means whatever the language config marks mastered",
    )
    parser.add_argument('--dry-run', action='store_true', help="Print the counts, suspend nothing")
    parser.add_argument('--url', default=anki.ANKI_CONNECT_URL, help="AnkiConnect endpoint")
    language_config.add_language_arguments(parser)
    arguments = parser.parse_args()

    root = language_config.PROJECT_ROOT
    language = language_config.language_from(arguments, root=root)
    levels = (
        {part.strip().upper() for part in arguments.levels.split(',') if part.strip()}
        if arguments.levels
        else set(language.mastered_levels)
    )
    if not levels:
        raise SystemExit("no level is marked mastered; name them with --levels")

    main_for(language=language, root=root, levels=levels, dry_run=arguments.dry_run, url=arguments.url)


if __name__ == '__main__':
    main()
