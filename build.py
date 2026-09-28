"""Build a deck from the word list you keep, in one command per language.

You add lines to words/<language>.txt and run this; it carries the words as far as it can and
tells you what is left. Every stage is idempotent and skips what is already done, so running it
again after adding five words costs almost nothing and cannot damage the work already paid for.

Three of the stages need a model and this script cannot call one on its own. Where those have
work outstanding it writes the task files out and stops naming them, rather than guessing or
silently leaving cards half-built.
"""

import argparse
import pathlib
import sys
import typing

from pipeline import anki
from pipeline import dictionaries
from pipeline import images
from pipeline import language_config
from pipeline import media
from pipeline import ordering
from pipeline import model
from pipeline import sense_choice
from pipeline import translations
from pipeline import wordlist

WORDLIST_DIRECTORY = 'words'
WORDS_FILE = 'words.tsv'
ENRICHED_FILE = 'enriched.tsv'
CARDS_FILE = 'cards.tsv'
AUDIO_ONLY = 'audio'


class Stage(typing.NamedTuple):
    """A step of the build, and what it is waiting for when it cannot finish."""

    name: str
    done: int
    pending: int
    blocked_on: str = ''


class DeckBuild:
    """Runs the stages in order, each one picking up what the previous left."""

    def __init__(
        self, *, language: language_config.LanguageConfig, root: pathlib.Path, with_images: bool = False,
    ):
        self._language = language
        self._root = root
        self._data = language.data_directory(root=root)
        self._with_images = with_images

    @property
    def wordlist_path(self) -> pathlib.Path:
        return self._root / WORDLIST_DIRECTORY / f'{self._language.target}.txt'

    def collect_words(self) -> Stage:
        """Merge the hand-kept list into the table, leaving words already there alone."""
        path = self.wordlist_path
        words_path = self._data / WORDS_FILE
        if not path.exists():
            existing = language_config.TsvFile.read(words_path) if words_path.exists() else []
            return Stage(
                name='words',
                done=len(existing),
                pending=0,
                blocked_on='' if existing else f"write words into {path}",
            )
        importer = wordlist.WordlistImporter(language=self._language)
        summary = importer.import_file(path=path, delimiter=wordlist.DEFAULT_DELIMITER)
        existing = language_config.TsvFile.read(words_path) if words_path.exists() else []
        rows, added = importer.merge_into(existing=existing, imported=summary.rows)
        if added:
            language_config.TsvFile.write(
                words_path, rows=rows, columns=self._language.extracted_columns,
            )
        return Stage(name='words', done=len(rows), pending=0)

    def enrich_from_dictionaries(self) -> Stage:
        """Free and deterministic, so it simply runs; its own cache makes a re-run cheap."""
        rows = self._read(name=WORDS_FILE)
        if not rows:
            return Stage(name='dictionaries', done=0, pending=0)
        enriched_path = self._data / ENRICHED_FILE
        known = {row['key'] for row in self._read(name=ENRICHED_FILE)}
        missing = [row for row in rows if row['key'] not in known]
        if missing:
            dictionaries.main_for(language=self._language, root=self._root)
        enriched = self._read(name=ENRICHED_FILE)
        return Stage(name='dictionaries', done=len(enriched), pending=0)

    def choose_senses(self) -> Stage:
        """Must clear before the model stage: a card built on the wrong sense can't be fixed downstream."""
        rows = self._read(name=WORDS_FILE)
        if not rows:
            return Stage(name='senses', done=0, pending=0)
        directory = self._data / sense_choice.TASK_DIRECTORY
        if any(directory.glob('answer-*.json')):
            sense_choice.main_for(language=self._language, root=self._root, from_json=directory)
            dictionaries.main_for(language=self._language, root=self._root)
            rows = self._read(name=WORDS_FILE)
        built_keys = {row['key'] for row in self._read(name=CARDS_FILE)}
        cache = dictionaries.Cache(
            root=self._data / dictionaries.CACHE_DIRECTORY_NAME,
            refresh=False,
            edition=self._language.wiktionary_host,
        )
        choice = sense_choice.SenseChoice(language=self._language, cache=cache)
        pending = len(choice.build(rows=rows, built_keys=built_keys))
        if pending:
            sense_choice.main_for(language=self._language, root=self._root, plan=True)
        return Stage(
            name='senses',
            done=len(rows) - pending,
            pending=pending,
            blocked_on=f"answer the task files in {directory}" if pending else '',
        )

    def enrich_with_model(self) -> Stage:
        """Cannot run headless: merge whatever answers are waiting, then write out the rest."""
        source = self._read(name=ENRICHED_FILE)
        if not source:
            return Stage(name='model', done=0, pending=0)
        directory = self._data / 'llm'
        if any(directory.glob('*.json')):
            model.main_for(language=self._language, root=self._root, from_json=directory)
        cards = self._read(name=CARDS_FILE)
        answered = sum(1 for row in cards if row.get('cefr'))
        pending = len(source) - answered
        if pending > 0:
            model.main_for(language=self._language, root=self._root, plan=True)
        return Stage(
            name='model',
            done=answered,
            pending=max(pending, 0),
            blocked_on=f"answer the task files in {directory}" if pending > 0 else '',
        )

    def complete_translations(self) -> Stage:
        """Same shape as the model stage: merge what is answered, plan what is not."""
        cards = self._read(name=CARDS_FILE)
        if not cards:
            return Stage(name='translations', done=0, pending=0)
        directory = self._data / translations.TASK_DIRECTORY
        if any(directory.glob('answer-*.json')):
            translations.main_for(language=self._language, root=self._root, from_json=directory)
        plan = translations.TranslationPlan(language=self._language)
        pending = len(plan.build(rows=self._read(name=CARDS_FILE)))
        if pending:
            translations.main_for(language=self._language, root=self._root, plan=True)
        return Stage(
            name='translations',
            done=len(cards) - pending,
            pending=pending,
            blocked_on=f"answer the task files in {directory}" if pending else '',
        )

    def gather_media(self) -> Stage:
        """Pronunciation always, pictures only when asked: most decks want the sound and not the
        pictures, and an image lookup is the slowest part of a run."""
        media.main_for(
            language=self._language, root=self._root, only=None if self._with_images else AUDIO_ONLY,
        )
        manifest = language_config.TsvFile.read(self._data / media.MANIFEST_FILENAME)
        with_audio = sum(1 for row in manifest if row.get('audio'))
        return Stage(name='media', done=with_audio, pending=0)

    def push(self) -> Stage:
        cards = self._read(name=CARDS_FILE)
        if not cards:
            return Stage(name='anki', done=0, pending=0)
        try:
            added, updated = anki.main_for(language=self._language, root=self._root)
        except anki.AnkiConnectError as error:
            return Stage(name='anki', done=0, pending=len(cards), blocked_on=str(error))
        return Stage(name='anki', done=added + updated, pending=0)

    def order_queue(self) -> Stage:
        """Positions only exist once the cards do, so this follows the push rather than the table."""
        try:
            moved = ordering.main_for(language=self._language)
        except anki.AnkiConnectError as error:
            return Stage(name='ordering', done=0, pending=0, blocked_on=str(error))
        return Stage(name='ordering', done=moved, pending=0)

    def _read(self, *, name: str) -> typing.List[dict]:
        path = self._data / name
        return language_config.TsvFile.read(path) if path.exists() else []


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--skip-media', action='store_true', help="Leave pronunciation and pictures alone this run",
    )
    parser.add_argument(
        '--images', action='store_true',
        help="Also look for pictures; off by default, since most words do not want one",
    )
    parser.add_argument(
        '--no-push', action='store_true', help="Stop before sending anything to Anki",
    )
    language_config.add_language_arguments(parser)
    arguments = parser.parse_args()

    root = language_config.PROJECT_ROOT
    language = language_config.language_from(arguments, root=root)
    build = DeckBuild(language=language, root=root, with_images=arguments.images)

    steps = [build.collect_words, build.enrich_from_dictionaries, build.choose_senses,
             build.enrich_with_model, build.complete_translations]
    if not arguments.skip_media:
        steps.append(build.gather_media)
    if not arguments.no_push:
        steps.append(build.push)
        steps.append(build.order_queue)

    blocked = []
    for step in steps:
        stage = step()
        suffix = f" — {stage.pending} pending" if stage.pending else ''
        print(f"{stage.name:14} {stage.done}{suffix}")
        if stage.blocked_on:
            blocked.append(f"{stage.name}: {stage.blocked_on}")
        if step is build.choose_senses and stage.blocked_on:
            break

    if blocked:
        print("\nwaiting on:", file=sys.stderr)
        for line in blocked:
            print(f"  {line}", file=sys.stderr)


if __name__ == '__main__':
    main()
