"""Fill a card's phonetic transcription from the language's own Wiktionary edition.

A word list exported from a vocabulary app carries a transcription per entry; a word taken from a
book carries nothing, so those cards reach the deck without one. Wiktionary states it for almost
every headword, and the page is already fetched and cached for the definition.

A transcription template names the language it belongs to, so a page shared by several languages
needs no section splitting here — the wrong language's line simply does not match.
"""

import argparse
import pathlib
import re
import typing

from . import dictionaries
from . import language_config

CARDS_FILENAME = 'cards.tsv'
IPA_TEMPLATE_PATTERN = re.compile(r'\{\{IPA\|([^{}]*)\}\}')
# The broad transcription between slashes, which is the one a learner wants; the narrow one in
# brackets records a single accent's detail and is noise on a card.
BROAD_TRANSCRIPTION_PATTERN = re.compile(r'/([^/|\]]{2,})/')


class Transcription:
    """What an edition states between slashes for a headword, in the language being learned."""

    @staticmethod
    def from_wikitext(*, wikitext: str, language_code: str) -> str:
        for template in IPA_TEMPLATE_PATTERN.finditer(wikitext):
            body = template.group(1)
            if not body.startswith(f'{language_code}|') and f'lang={language_code}' not in body:
                continue
            found = BROAD_TRANSCRIPTION_PATTERN.search(body)
            if found is not None:
                return found.group(1)
        return ''


def main_for(
    *,
    language: language_config.LanguageConfig,
    root: pathlib.Path,
    subdeck: typing.Optional[str] = None,
    dry_run: bool = False,
) -> None:
    data_directory = language.data_directory(root=root)
    table_path = data_directory / CARDS_FILENAME
    columns = table_path.read_text(encoding='utf-8').split('\n', 1)[0].split('\t')
    rows = language_config.TsvFile.read(table_path)

    pending = [
        row for row in rows
        if not row.get('ipa', '').strip() and not row.get('merged_into', '').strip()
        and (subdeck is None or row.get('subdeck', '') == subdeck)
    ]
    if not pending:
        print("every card already carries a transcription")
        return

    cache = dictionaries.Cache(root=data_directory / dictionaries.CACHE_DIRECTORY_NAME, refresh=False)
    stats = dictionaries.Stats()
    lemmas = [row['word'].strip().lower() for row in pending]
    dictionaries.WiktionaryClient.fetch_many(lemmas=lemmas, cache=cache, language=language, stats=stats)

    filled = 0
    for row in pending:
        cached = cache.load(source=dictionaries.Source.WIKTIONARY, lemma=row['word'].strip().lower())
        if cached is None:
            continue
        transcription = Transcription.from_wikitext(
            wikitext=cached.get('wikitext', ''), language_code=language.target,
        )
        if transcription:
            row['ipa'] = transcription
            filled += 1

    print(f"{filled} of {len(pending)} cards got a transcription")
    if dry_run:
        print("dry run: wrote nothing")
        return
    language_config.TsvFile.write(table_path, rows=rows, columns=columns)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--subdeck', default=None, help="Fill only the cards filed under this subdeck")
    parser.add_argument('--dry-run', action='store_true', help="Print the counts, write nothing")
    language_config.add_language_arguments(parser)
    arguments = parser.parse_args()

    root = language_config.PROJECT_ROOT
    main_for(
        language=language_config.language_from(arguments, root=root),
        root=root,
        subdeck=arguments.subdeck,
        dry_run=arguments.dry_run,
    )


if __name__ == '__main__':
    main()
