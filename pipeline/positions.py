"""Order the new-card queue so a learner meets useful words before rare ones.

Splitting the deck into CEFR subdecks (see `language_config.LanguageConfig.splits_deck_by_cefr`)
already gets the levels themselves into order, because Anki gathers new cards subdeck by subdeck
in name order. Within one subdeck, though, new cards still come out in whatever order the source
app's table happened to hold them in — which starts with that app's anatomy block, so a learner
meets `bowel` before `hello`. This stage fixes that by giving every new card a `due` value that
reflects the word's own frequency, so the queue itself carries the ordering CEFR splitting cannot.

`due` is what AnkiConnect calls a new card's queue position, and this AnkiConnect build has no
dedicated reposition action — `repositionCards` is the one every guide points to, but it is
absent here, and `answers` accepted by this install is limited to what `apiReflect` actually
lists. `setSpecificValueOfCard` with `keys=['due']` is what is verified to work against this
install, so that is what this stage uses; a no-op write (value already correct) returns `[True]`
just like a real one, which is why a card already in place is filtered out before the call rather
than trusted to the response.

Only a card that has never been studied (`type` 0) is ever touched. A card the learner has
already studied carries their own scheduling history in its `due` value — reinterpreting that
value as a queue position would silently discard it, so a studied card is left alone no matter
where it falls in the new order.

A suspended card is still repositioned. Suspending hides a card, it does not spend it: the card
keeps its position and uses it the moment the learner brings it back, and leaving it behind would
strand a whole level at whatever order it happened to have when it was hidden.
"""

import argparse
import pathlib
import typing

from . import anki
from . import frequency
from . import language_config

CARDS_FILENAME = 'cards.tsv'
PREVIEW_WORD_COUNT = 20

# New in Anki's own scheme; a card of any other type carries a schedule instead of a position.
NEW_CARD_TYPE = 0

# CEFR level order the deck already studies in; a row left unleveled sorts after all of them.
CEFR_LEVELS = ('A1', 'A2', 'B1', 'B2', 'C1', 'C2')
UNLEVELED_RANK = len(CEFR_LEVELS)


def level_rank(*, level: str) -> int:
    level = level.strip().upper()
    return CEFR_LEVELS.index(level) if level in CEFR_LEVELS else UNLEVELED_RANK


def ordered_rows(*, rows: typing.List[dict], scores: frequency.FrequencyBands) -> typing.List[dict]:
    """Rows sorted level first, then most useful word first, ties broken by the word itself."""
    return sorted(
        rows,
        key=lambda row: (
            level_rank(level=row.get('cefr', '')),
            -scores.score_for(word=row.get('word', '')),
            row.get('word', ''),
        ),
    )


def positions_by_key(*, rows: typing.List[dict]) -> typing.Dict[str, int]:
    """Every row's queue position, one-based and stable across runs given the same table."""
    return {row['key']: position for position, row in enumerate(rows, start=1)}


class Repositioner:
    """Moves this language's still-new cards to the `due` value their word's rank calls for."""

    def __init__(self, *, client: anki.AnkiConnect, language: language_config.LanguageConfig):
        self._client = client
        self._language = language

    def cards_by_key(self) -> typing.Dict[str, typing.List[dict]]:
        """Every card of this language's note type, grouped by the key its note carries.

        Filtering by note type rather than by deck is what keeps this stage off any other
        language's cards without having to name every subdeck the CEFR split might create.
        """
        note_ids = self._client.invoke(action='findNotes', query=f'note:"{self._language.note_type_name}"')
        if not note_ids:
            return {}
        notes = self._client.invoke(action='notesInfo', notes=note_ids)
        card_ids = [card_id for note in notes for card_id in note['cards']]
        cards = {info['cardId']: info for info in self._client.invoke(action='cardsInfo', cards=card_ids)}
        by_key: typing.Dict[str, typing.List[dict]] = {}
        for note in notes:
            key = note['fields'].get('Key', {}).get('value', '')
            if not key:
                continue
            by_key[key] = [cards[card_id] for card_id in note['cards'] if card_id in cards]
        return by_key

    def reposition(self, *, card_id: int, position: int) -> None:
        self._client.invoke(
            action='setSpecificValueOfCard',
            card=card_id,
            keys=['due'],
            newValues=[position],
            warning_check=True,
        )


def main_for(
    *,
    language: language_config.LanguageConfig,
    root: pathlib.Path,
    dry_run: bool = False,
    limit: typing.Optional[int] = None,
    url: str = anki.ANKI_CONNECT_URL,
) -> None:
    cards_path = language.data_directory(root=root) / CARDS_FILENAME
    rows = language_config.TsvFile.read(cards_path)
    scores = frequency.FrequencyBands(language_code=language.target)
    ordered = ordered_rows(rows=rows, scores=scores)
    if limit is not None:
        ordered = ordered[:limit]
    positions = positions_by_key(rows=ordered)

    if dry_run:
        preview = ', '.join(row.get('word', '') for row in ordered[:PREVIEW_WORD_COUNT])
        print(f"new order starts: {preview}")

    client = anki.AnkiConnect(url=url)
    repositioner = Repositioner(client=client, language=language)
    cards_by_key = repositioner.cards_by_key()

    repositioned, studied, already_placed = 0, 0, 0
    for key, position in positions.items():
        for card in cards_by_key.get(key, []):
            if card['type'] != NEW_CARD_TYPE:
                studied += 1
                continue
            if card['due'] == position:
                already_placed += 1
                continue
            repositioned += 1
            if not dry_run:
                repositioner.reposition(card_id=card['cardId'], position=position)

    print(
        f"repositioned {repositioned} | left alone (has review history) {studied} | "
        f"already in place {already_placed}"
    )
    if dry_run:
        print("dry run: wrote nothing")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true', help="Print the summary and preview, write nothing")
    parser.add_argument('--limit', type=int, default=None, help="Reposition only the first N words in the new order")
    parser.add_argument('--url', default=anki.ANKI_CONNECT_URL, help="AnkiConnect endpoint")
    language_config.add_language_arguments(parser)
    arguments = parser.parse_args()

    root = language_config.PROJECT_ROOT
    main_for(
        language=language_config.language_from(arguments, root=root),
        root=root,
        dry_run=arguments.dry_run,
        limit=arguments.limit,
        url=arguments.url,
    )


if __name__ == '__main__':
    main()
