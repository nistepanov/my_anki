"""Put the new-card queue of each subdeck into the order a learner wants to meet the words in.

Anki hands out new cards by their position, and a position is just the order the note was
added. That makes the queue a record of how the deck was built rather than of what is worth
learning first: an import run appends a thousand words to the back, so words a learner meets
constantly wait behind rarities that happened to arrive earlier.

Splitting the deck by level fixes the coarse half of this — the levels come in order because
Anki gathers subdeck by subdeck in name order. Within one level nothing orders the words at
all, and a level holds well over a year of study at a normal daily limit, so the order inside
it decides most of what a learner actually sees.

Frequency is the ordering worth having: within a level, the commoner word earns its place
first. Words the frequency lists never ranked go last, since an unranked word is either rare
or too specialised for the lists to have counted it.

Rewriting positions is safe only for cards nobody has started. A card in review carries its
schedule in the same field, so this touches new cards only, and it keeps the current relative
order inside a band so that re-running after an import moves the newcomers into place instead
of reshuffling everything.
"""

import argparse
import collections
import pathlib
import typing

from . import anki
from . import language_config

# Frequency bands in the order a learner should meet them. A word the lists never ranked
# sorts after every ranked one.
BAND_TAG_PREFIX = 'freq::'
BAND_ORDER = ('top100', 'top1000', 'top3000', 'top5000')
UNRANKED_RANK = len(BAND_ORDER)
# AnkiConnect guards direct card writes behind this flag; the guard is the point of the flag.
WARNING_ACKNOWLEDGED = True
# One request per card is thousands of round trips, so they go in batches.
BATCH_SIZE = 200


class NewCardQueue:
    """The new cards of one deck tree, grouped by subdeck and rankable by frequency."""

    def __init__(self, *, cards: typing.List[dict], notes: typing.Dict[int, dict]):
        self._cards = cards
        self._notes = notes

    @classmethod
    def load(cls, *, client: anki.AnkiConnect, deck_name: str) -> 'NewCardQueue':
        card_ids = client.invoke(action='findCards', query=f'"deck:{deck_name}" is:new')
        if not card_ids:
            return cls(cards=[], notes={})
        cards = client.invoke(action='cardsInfo', cards=card_ids)
        note_ids = sorted({card['note'] for card in cards})
        notes = {note['noteId']: note for note in client.invoke(action='notesInfo', notes=note_ids)}
        return cls(cards=cards, notes=notes)

    def __len__(self) -> int:
        return len(self._cards)

    def band_rank_of(self, *, card: dict) -> int:
        tags = self._notes.get(card['note'], {}).get('tags', ())
        for tag in tags:
            if not tag.startswith(BAND_TAG_PREFIX):
                continue
            band = tag[len(BAND_TAG_PREFIX):]
            if band in BAND_ORDER:
                return BAND_ORDER.index(band)
        return UNRANKED_RANK

    def positions(self) -> typing.Dict[int, int]:
        """Card id to the position it should hold, numbered from the front of its own subdeck.

        Both cards of a note take the same position: they are the same word met two ways, and
        keeping them together lets the deck's own sort order decide which side comes first.
        """
        by_subdeck: typing.Dict[str, typing.List[dict]] = collections.defaultdict(list)
        for card in self._cards:
            by_subdeck[card['deckName']].append(card)

        wanted: typing.Dict[int, int] = {}
        for cards in by_subdeck.values():
            ranked = sorted(cards, key=lambda card: (self.band_rank_of(card=card), card['due']))
            position_of_note: typing.Dict[int, int] = {}
            for card in ranked:
                position = position_of_note.setdefault(card['note'], len(position_of_note))
                wanted[card['cardId']] = position
        return wanted

    def misplaced(self) -> typing.Dict[int, int]:
        """Only the cards whose position has to change, so a settled deck costs nothing."""
        wanted = self.positions()
        current = {card['cardId']: card['due'] for card in self._cards}
        return {card_id: position for card_id, position in wanted.items() if current[card_id] != position}

    def spread(self) -> collections.Counter:
        return collections.Counter(
            (card['deckName'], BAND_ORDER[rank] if rank < len(BAND_ORDER) else 'unranked')
            for card in self._cards
            for rank in (self.band_rank_of(card=card),)
        )


class Repositioner:
    """Writes new-card positions back to Anki."""

    def __init__(self, *, client: anki.AnkiConnect):
        self._client = client

    def apply(self, *, positions: typing.Dict[int, int]) -> int:
        items = sorted(positions.items())
        for start in range(0, len(items), BATCH_SIZE):
            batch = items[start:start + BATCH_SIZE]
            self._client.invoke_many(actions=[
                {
                    'action': 'setSpecificValueOfCard',
                    'version': 6,
                    'params': {
                        'card': card_id,
                        'keys': ['due'],
                        'newValues': [str(position)],
                        'warning_check': WARNING_ACKNOWLEDGED,
                    },
                }
                for card_id, position in batch
            ])
        return len(items)


def main_for(
    *,
    language: language_config.LanguageConfig,
    url: str = anki.ANKI_CONNECT_URL,
    dry_run: bool = False,
) -> int:
    client = anki.AnkiConnect(url=url)
    queue = NewCardQueue.load(client=client, deck_name=language.deck_name)
    if not len(queue):
        print(f"no new cards in {language.deck_name!r} — nothing to order")
        return 0

    misplaced = queue.misplaced()
    print(f'{len(queue)} new cards in {language.deck_name!r}, {len(misplaced)} out of place')
    for (deck, band), count in sorted(queue.spread().items()):
        print(f'  {deck:<24} {band:<10} {count}')
    if dry_run or not misplaced:
        return len(misplaced)

    moved = Repositioner(client=client).apply(positions=misplaced)
    print(f'repositioned {moved} cards — commonest words now come first inside each subdeck')
    return moved


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default=anki.ANKI_CONNECT_URL, help="AnkiConnect endpoint")
    parser.add_argument('--dry-run', action='store_true', help="Report what would move, move nothing")
    language_config.add_language_arguments(parser)
    arguments = parser.parse_args()

    main_for(
        language=language_config.language_from(arguments, root=language_config.PROJECT_ROOT),
        url=arguments.url,
        dry_run=arguments.dry_run,
    )


if __name__ == '__main__':
    main()
