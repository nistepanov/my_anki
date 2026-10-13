"""Fill a noun's gender and article from the dictionary page already in the cache.

A word list made of bare headwords, such as a graded lexicon, says nothing about gender. For a
language where the article carries the gender, a noun card without it teaches half the word.
The dictionary entry states the gender next to the headword, so it is read from there.

Which marks mean which gender, and which article goes with it, are facts about the language,
so they come from its config; a language with no `noun_gender` entry is left alone.
"""

import argparse
import pathlib
import re
import typing

from . import dictionaries
from . import language_config

FILES_TO_FILL = ('words.tsv', 'enriched.tsv', 'cards.tsv')
NOUN = 'noun'


class GenderReader:
    """Reads the gender a dictionary entry states for one noun."""

    def __init__(self, *, language: language_config.LanguageConfig, cache: dictionaries.Cache):
        settings = language.noun_gender
        self._language = language
        self._cache = cache
        self._noun_heading = re.compile(settings['noun_heading'], re.MULTILINE)
        self._gender_mark = re.compile(settings['gender_mark'])
        self._genders = settings['genders']
        self._articles = settings['articles']
        self._elided_article = settings.get('elided_article', '')
        self._elision_letters = settings.get('elision_letters', '')

    def gender_of(self, *, word: str) -> str:
        """The gender stated on the first headword line under the first noun heading, or ''."""
        cached = self._cache.load(source=dictionaries.Source.WIKTIONARY, lemma=word)
        if not cached or not cached.get('found'):
            return ''
        section = dictionaries.WiktionaryClient._extract_language_section(
            wikitext=cached.get('wikitext', ''), language=self._language,
        )
        heading = self._noun_heading.search(section)
        if heading is None:
            return ''
        for line in section[heading.end():].splitlines():
            if line.startswith("'''"):
                mark = self._gender_mark.search(line)
                return self._genders.get(mark.group(1), '') if mark else ''
        return ''

    def article_for(self, *, word: str, gender: str) -> str:
        if self._elided_article and word[:1].lower() in self._elision_letters:
            return self._elided_article
        return self._articles.get(gender, '')


def fill_rows(*, rows: typing.List[dict], reader: GenderReader) -> int:
    """Fill gender and article on nouns that have neither; returns how many were filled."""
    filled = 0
    for row in rows:
        if row.get('part_of_speech') != NOUN or row.get('gender') or row.get('article'):
            continue
        gender = reader.gender_of(word=row['word'])
        if not gender:
            continue
        row['gender'] = gender
        row['article'] = reader.article_for(word=row['word'], gender=gender)
        filled += 1
    return filled


def main_for(*, language: language_config.LanguageConfig, root: pathlib.Path) -> None:
    if not language.noun_gender:
        return
    data_directory = language.data_directory(root=root)
    cache = dictionaries.Cache(
        root=data_directory / dictionaries.CACHE_DIRECTORY_NAME,
        refresh=False,
        edition=language.wiktionary_host,
    )
    reader = GenderReader(language=language, cache=cache)
    for name in FILES_TO_FILL:
        path = data_directory / name
        if not path.exists():
            continue
        rows = language_config.TsvFile.read(path)
        filled = fill_rows(rows=rows, reader=reader)
        if filled:
            columns = language.card_columns if name == 'cards.tsv' else list(rows[0])
            language_config.TsvFile.write(path, rows=rows, columns=columns)
        print(f"{name}: filled gender on {filled} nouns")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    language_config.add_language_arguments(parser)
    arguments = parser.parse_args()
    root = language_config.PROJECT_ROOT
    main_for(language=language_config.language_from(arguments, root=root), root=root)


if __name__ == '__main__':
    main()
