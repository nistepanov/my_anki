"""Write a deck file that other people may have: only openly licensed content reaches it.

The personal deck also holds material that may be studied but not passed on: a vocabulary
app's sentences and pictures, a publisher's word lists, unofficial speech endpoints. A field
reaches the shared deck only when its recorded source allows that. An unknown source counts as
closed, so a field the pipeline cannot trace is left out rather than guessed.

Recordings never go in. The card speaks the word with Anki's own text-to-speech instead.
"""

import argparse
import dataclasses
import hashlib
import html
import pathlib
import re
import sys
import typing

import genanki

from . import anki
from . import language_config

# Source label to its credit. An empty credit means the model wrote it, which needs none.
OPEN_DEFINITION_SOURCES = {
    'wiktionary': "Wiktionary, CC BY-SA 4.0",
    'primary sense review': "Wiktionary, CC BY-SA 4.0",
    'llm': '',
}
OPEN_EXAMPLE_SOURCES = {
    'tatoeba': "Tatoeba, CC BY 2.0 FR",
    'llm': '',
}
OPEN_LEVEL_SOURCES = {
    'cefrj': "CEFR-J Wordlist 1.5, Yukio Tono, Tokyo University of Foreign Studies",
    'octanove': "Octanove Vocabulary Profile C1/C2, CC BY-SA 4.0",
    'efllex': "EFLLex, CC BY-NC-SA 4.0",
    'elelex': "ELELex, CC BY-NC-SA 4.0",
    '': '',
}
NON_COMMERCIAL_LEVEL_SOURCES = frozenset({'efllex', 'elelex'})
OPEN_IMAGE_SOURCES = frozenset({'openverse', 'wikipedia'})
# Licences that allow anyone to copy the file unchanged; NC and ND variants are left out.
FREE_IMAGE_LICENCE_PATTERN = re.compile(r'^(cc0|pdm|public domain|(cc[ -])?by(-sa)?([ -]\d.*)?)$', re.IGNORECASE)

LEVEL_TAG_PREFIX = 'cefr::'
# Tags about the author's own study, not about the word.
PERSONAL_TAGS = frozenset({'studied'})
PERSONAL_TAG_PREFIXES = ('source::',)
PRONUNCIATION_FIELD = 'Pronunciation'
SPOKEN_FIELD = 'Word'
TTS_FALLBACK_SCRIPT = language_config.PROJECT_ROOT / anki.TEMPLATE_DIRECTORY / 'tts_fallback.js'
CREDITS_FIELD = 'Credits'
SHARE_ALIKE_LICENCE = "CC BY-SA 4.0"
NON_COMMERCIAL_LICENCE = "CC BY-NC-SA 4.0"
PROJECT_URL = 'https://github.com/nistepanov/my_anki'
# Anki names a voice by locale; a bare language code is the fallback for any other language.
TTS_LOCALES = {
    'en': 'en_US', 'es': 'es_ES', 'de': 'de_DE', 'ru': 'ru_RU', 'fr': 'fr_FR', 'it': 'it_IT',
    'pt': 'pt_PT', 'nl': 'nl_NL', 'pl': 'pl_PL', 'sv': 'sv_SE', 'tr': 'tr_TR', 'cs': 'cs_CZ',
}
# genanki asks for ids in this range so they do not clash with ids Anki makes itself.
ANKI_ID_FLOOR = 1 << 30
ANKI_ID_SPAN = 1 << 30
EXPORT_FILE_SUFFIX = '.apkg'
BOOK_SUBDECK_COLUMN = 'subdeck'
# Tatoeba sentences kept beside a closed example set, for the shared deck only.
OPEN_EXAMPLES_PREFIX = 'open_'


@dataclasses.dataclass
class ExportStats:
    notes: int = 0
    definitions_dropped: int = 0
    examples_dropped: int = 0
    levels_dropped: int = 0
    images_kept: int = 0
    images_dropped: int = 0
    non_commercial: bool = False


class SharedRow:
    """One table row with every closed field emptied, plus the credits its open fields need."""

    @staticmethod
    def clean(
        *, row: dict, language: language_config.LanguageConfig, stats: ExportStats,
    ) -> typing.Tuple[dict, typing.List[str]]:
        cleaned = dict(row)
        credits: typing.List[str] = []
        # Dictionary examples are not on the card, and their source is not recorded per sentence.
        cleaned['dictionary_examples'] = ''
        cleaned['tags'] = SharedRow.public_tags(tags=row.get('tags', ''))
        SharedRow._keep_definition(row=cleaned, language=language, credits=credits, stats=stats)
        SharedRow._keep_examples(row=cleaned, language=language, credits=credits, stats=stats)
        SharedRow._keep_level(row=cleaned, credits=credits, stats=stats)
        return cleaned, credits

    @staticmethod
    def _keep_definition(
        *, row: dict, language: language_config.LanguageConfig, credits: typing.List[str], stats: ExportStats,
    ) -> None:
        columns = (
            language.definition_column, *language.definition_translation_columns, language.definition_full_column,
        )
        if not row.get(language.definition_column):
            return
        source = row.get('definition_source', '')
        if source in OPEN_DEFINITION_SOURCES:
            SharedRow._credit(credits=credits, label="Definition", credit=OPEN_DEFINITION_SOURCES[source])
            return
        for column in columns:
            row[column] = ''
        stats.definitions_dropped += 1

    @staticmethod
    def _keep_examples(
        *, row: dict, language: language_config.LanguageConfig, credits: typing.List[str], stats: ExportStats,
    ) -> None:
        if not any(row.get(column) for column in language.example_columns):
            return
        source = row.get('examples_source', '')
        if source in OPEN_EXAMPLE_SOURCES:
            SharedRow._credit(credits=credits, label="Examples", credit=OPEN_EXAMPLE_SOURCES[source])
            return
        # The sentence card is built on one of these sentences, so it goes with them.
        for column in (*language.example_columns, 'context_index', 'context_target'):
            row[column] = ''
        if SharedRow._use_open_examples(row=row, language=language):
            SharedRow._credit(credits=credits, label="Examples", credit=OPEN_EXAMPLE_SOURCES['tatoeba'])
            return
        stats.examples_dropped += 1

    @staticmethod
    def _use_open_examples(*, row: dict, language: language_config.LanguageConfig) -> bool:
        """Put in sentences found for the shared deck alone, where the personal deck's own may not be shared."""
        target_column, native_column = (
            language.examples_column(suffix=suffix) for suffix in (language.target, language.native)
        )
        open_target = row.get(f'{OPEN_EXAMPLES_PREFIX}{target_column}', '')
        open_native = row.get(f'{OPEN_EXAMPLES_PREFIX}{native_column}', '')
        # Sentences without the reader's translation are not worth a card.
        if not open_target or not open_native:
            return False
        row[target_column] = open_target
        row[native_column] = open_native
        return True

    @staticmethod
    def _keep_level(*, row: dict, credits: typing.List[str], stats: ExportStats) -> None:
        if not row.get('cefr'):
            return
        source = row.get('level_source', '')
        if source in OPEN_LEVEL_SOURCES:
            SharedRow._credit(credits=credits, label="Level", credit=OPEN_LEVEL_SOURCES[source])
            stats.non_commercial = stats.non_commercial or source in NON_COMMERCIAL_LEVEL_SOURCES
            return
        row['cefr'] = ''
        row['tags'] = ' '.join(tag for tag in row.get('tags', '').split() if not tag.startswith(LEVEL_TAG_PREFIX))
        stats.levels_dropped += 1

    @staticmethod
    def public_tags(*, tags: str) -> str:
        return ' '.join(
            tag for tag in tags.split()
            if tag not in PERSONAL_TAGS and not tag.startswith(PERSONAL_TAG_PREFIXES)
        )

    @staticmethod
    def _credit(*, credits: typing.List[str], label: str, credit: str) -> None:
        if credit:
            credits.append(f"{label}: {credit}")


class SharedMedia:
    """Pictures with a free licence; recordings are never shipped."""

    def __init__(self, *, directory: pathlib.Path, entries: typing.Dict[str, dict]):
        self._directory = directory
        self._entries = entries
        self.files: typing.List[pathlib.Path] = []

    @classmethod
    def load(cls, *, language: language_config.LanguageConfig) -> 'SharedMedia':
        data_directory = language.data_directory(root=language_config.PROJECT_ROOT)
        manifest = data_directory / anki.MEDIA_MANIFEST
        entries = {entry['key']: entry for entry in language_config.TsvFile.read(manifest)} if manifest.exists() else {}
        return cls(directory=data_directory / anki.MEDIA_DIRECTORY, entries=entries)

    def fields_for(self, *, row: dict, credits: typing.List[str], stats: ExportStats) -> typing.Dict[str, str]:
        fields = {'Pronunciation': '', 'Image': ''}
        entry = self._entries.get(row['key'], {})
        if not entry.get('image'):
            return fields
        path = self._directory / entry['image']
        if not self.is_free(entry=entry) or not path.exists():
            stats.images_dropped += 1
            return fields
        fields['Image'] = f'<img src="{entry["image"]}">'
        attribution = entry.get('image_attribution', '').strip()
        credits.append(f"Image: {attribution or 'unknown author'}, {entry['image_license']}")
        self.files.append(path)
        stats.images_kept += 1
        return fields

    @staticmethod
    def is_free(*, entry: dict) -> bool:
        licence = entry.get('image_license', '').strip()
        return entry.get('image_source') in OPEN_IMAGE_SOURCES and bool(FREE_IMAGE_LICENCE_PATTERN.match(licence))


class SharedDeckWriter:
    """Builds the note type, the level subdecks and the notes, and writes one package file."""

    def __init__(self, *, language: language_config.LanguageConfig, deck_name: str):
        self._language = language
        self._deck_name = deck_name
        self._design = anki.CardDesign(
            directory=language_config.PROJECT_ROOT / anki.TEMPLATE_DIRECTORY,
            labels=language.labels,
            variant=language.target,
            card_templates=language.card_templates,
        )
        self._fields = [*anki.DeckBuilder.note_type_fields(language=language, design=self._design), CREDITS_FIELD]
        self._model = self._build_model()
        self._decks: typing.Dict[str, genanki.Deck] = {}

    def _build_model(self) -> genanki.Model:
        # Its own name, so importing the shared deck never touches the author's note type.
        name = f'{self._language.note_type_name} (shared{self.reader_suffix})'
        return genanki.Model(
            model_id=self.stable_id(name=name),
            name=name,
            fields=[{'name': field} for field in self._fields],
            templates=[
                {
                    'name': template,
                    'qfmt': self.speak_with_tts(text=sides['Front']),
                    'afmt': self.speak_with_tts(text=sides['Back']),
                }
                for template, sides in self._design.templates.items()
            ],
            css=self._design.css,
        )

    def speak_with_tts(self, *, text: str) -> str:
        """The phone's own voice reads the word, so the deck carries no recording."""
        locale = TTS_LOCALES.get(self._language.target, self._language.target)
        spoken = f'<span class="tts-fallback">{{{{tts {locale}:{SPOKEN_FIELD}}}}}</span>'
        if f'{{{{{PRONUNCIATION_FIELD}}}}}' in text:
            # AnkiWeb cannot speak and shows the tag as text; the script fixes that card by card.
            text += f'\n<script>{TTS_FALLBACK_SCRIPT.read_text(encoding="utf-8")}</script>'
        return (
            text.replace(f'{{{{#{PRONUNCIATION_FIELD}}}}}', f'{{{{#{SPOKEN_FIELD}}}}}')
            .replace(f'{{{{/{PRONUNCIATION_FIELD}}}}}', f'{{{{/{SPOKEN_FIELD}}}}}')
            .replace(f'{{{{{PRONUNCIATION_FIELD}}}}}', spoken)
        )

    def add(self, *, row: dict, fields: typing.Dict[str, str]) -> None:
        level = row.get('cefr', '').strip() or language_config.UNLEVELED_SUBDECK_NAME
        note = genanki.Note(
            model=self._model,
            fields=[fields.get(field, '') for field in self._fields],
            tags=row.get('tags', '').split(),
            guid=genanki.guid_for(row['key'], *self.reader_identity),
        )
        self._deck_for(name=f'{self._deck_name}::{level}').add_note(note)

    def _deck_for(self, *, name: str) -> genanki.Deck:
        if name not in self._decks:
            self._decks[name] = genanki.Deck(deck_id=self.stable_id(name=f'{name}{self.reader_suffix}'), name=name)
        return self._decks[name]

    def write(self, *, path: pathlib.Path, media_files: typing.Sequence[pathlib.Path], description: str) -> None:
        for deck in self._decks.values():
            deck.description = description
        path.parent.mkdir(parents=True, exist_ok=True)
        package = genanki.Package(list(self._decks.values()), media_files=[str(file) for file in media_files])
        package.write_to_file(str(path))

    @property
    def reader_identity(self) -> typing.Tuple[str, ...]:
        """Ids for another reader's deck differ, so importing it never overwrites the default reader's notes.

        The default reader has no suffix, which keeps the ids of decks already shared.
        """
        if self._language.native == language_config.DEFAULT_NATIVE:
            return ()
        return (self._language.native,)

    @property
    def reader_suffix(self) -> str:
        return ''.join(f', {native}' for native in self.reader_identity)

    @staticmethod
    def stable_id(*, name: str) -> int:
        """The same name gives the same id, so a new export updates an old import instead of copying it."""
        digest = int.from_bytes(hashlib.sha256(name.encode('utf-8')).digest()[:8], 'big')
        return ANKI_ID_FLOOR + digest % ANKI_ID_SPAN

    @staticmethod
    def description(*, stats: ExportStats) -> str:
        licence = NON_COMMERCIAL_LICENCE if stats.non_commercial else SHARE_ALIKE_LICENCE
        return (
            f'Built with <a href="{PROJECT_URL}">{PROJECT_URL}</a>. '
            "Definitions from Wiktionary, example sentences from Tatoeba, pictures from Openverse and "
            "Wikipedia, levels from published CEFR word lists; the rest was written by a language model. "
            "Each note names its sources in the Credits field. "
            f'This deck is shared under <a href="https://creativecommons.org/licenses/">{licence}</a>.'
        )


def export(
    *,
    language: language_config.LanguageConfig,
    deck_name: str,
    output: pathlib.Path,
    with_images: bool = True,
    with_books: bool = False,
    levels: typing.Optional[typing.Collection[str]] = None,
) -> ExportStats:
    rows = language_config.TsvFile.read(language.data_directory(root=language_config.PROJECT_ROOT) / 'cards.tsv')
    rows = [row for row in rows if not row.get(anki.MERGED_COLUMN, '').strip()]
    if not with_books:
        # A book's words sit in a subdeck named for the book.
        rows = [row for row in rows if not row.get(BOOK_SUBDECK_COLUMN, '').strip()]
    if levels is not None:
        rows = [row for row in rows if row.get('cefr', '').strip() in levels]
    stats = ExportStats()
    media = SharedMedia.load(language=language) if with_images else SharedMedia(directory=pathlib.Path(), entries={})
    writer = SharedDeckWriter(language=language, deck_name=deck_name)
    for row in rows:
        cleaned, credits = SharedRow.clean(row=row, language=language, stats=stats)
        fields = anki.NotePusher.render_fields(row=cleaned, language=language)
        fields.update(media.fields_for(row=cleaned, credits=credits, stats=stats))
        fields[CREDITS_FIELD] = '<br>'.join(html.escape(credit) for credit in credits)
        writer.add(row=cleaned, fields=fields)
        stats.notes += 1
    writer.write(path=output, media_files=media.files, description=SharedDeckWriter.description(stats=stats))
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--deck', default=None, help="Deck name in the shared file (default: '<Target> vocabulary')")
    parser.add_argument('--output', type=pathlib.Path, default=None, help="Where to write the .apkg file")
    parser.add_argument('--no-images', action='store_true', help="Leave every picture out")
    parser.add_argument('--with-books', action='store_true', help="Keep the words taken from books, which stay out by default")
    parser.add_argument('--levels', nargs='+', default=None, help="Keep only these CEFR levels, e.g. A1 A2 B1")
    language_config.add_language_arguments(parser)
    arguments = parser.parse_args()

    language = language_config.language_from(arguments, root=language_config.PROJECT_ROOT)
    deck_name = arguments.deck or f'{language.target_name} vocabulary'
    default_output = language.data_directory(root=language_config.PROJECT_ROOT) / f'{deck_name}{EXPORT_FILE_SUFFIX}'
    output = arguments.output or default_output
    stats = export(
        language=language,
        deck_name=deck_name,
        output=output,
        with_images=not arguments.no_images,
        with_books=arguments.with_books,
        levels=arguments.levels,
    )
    print(f"wrote {output} — {stats.notes} notes, {stats.images_kept} pictures")
    print(
        f"left out: {stats.definitions_dropped} definitions, {stats.examples_dropped} example sets, "
        f"{stats.levels_dropped} levels, {stats.images_dropped} pictures with no open licence",
        file=sys.stderr,
    )
    if stats.non_commercial:
        print(
            f"levels from a non-commercial list are included, so the deck is {NON_COMMERCIAL_LICENCE}",
            file=sys.stderr,
        )


if __name__ == '__main__':
    main()
