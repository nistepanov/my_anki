"""Push the finished card table into a running Anki via the AnkiConnect add-on.

Idempotent by design: notes are matched on the key field, so re-running updates rather
than duplicating. Anki silently refuses a note whose first field already exists, and a
silent refusal is indistinguishable from success — hence the lookup-then-decide flow and
the separate added/updated counts.
"""

import argparse
import base64
import html
import json
import pathlib
import re
import sys
import typing
import urllib.error
import urllib.request

from . import examples
from . import language_config
from . import senses

ANKI_CONNECT_URL = 'http://127.0.0.1:8765'
API_VERSION = 6
REQUEST_TIMEOUT_SECONDS = 60
ADD_BATCH_SIZE = 100
UPDATE_BATCH_SIZE = 50

TEMPLATE_DIRECTORY = 'templates'
STYLE_FILE = 'style.css'
# Anki would read a {{name}} placeholder as a field, so labels use their own marker.
LABEL_PATTERN = re.compile(r'%%(\w+)%%')
# A template placeholder, with the section markers and filter prefixes a field name may carry.
PLACEHOLDER_PATTERN = re.compile(r'\{\{([^{}]+)\}\}')
# Anki fills these itself; a note type that declares them as fields is rejected.
BUILT_IN_PLACEHOLDERS = frozenset({'FrontSide', 'Tags', 'Type', 'Deck', 'Subdeck', 'Card', 'CardFlag'})
# Both card types need the same script, and a copy in each is a fix that lands in only one.
INCLUDE_PATTERN = re.compile(r'%%include:([\w.]+)%%')

# Stands in for a letter the learner still has to produce; a dot reads as a gap, a dash as a
# hyphen the word might actually contain.
HINT_MASK_CHARACTER = '·'

MEDIA_DIRECTORY = 'media'
MEDIA_MANIFEST = 'media.tsv'
# Set on a row folded into another card; such a row is no longer a card to push.
MERGED_COLUMN = 'merged_into'


class AnkiConnectError(RuntimeError):
    """AnkiConnect answered, but reported a failure."""


class CardDesign:
    """Card templates and styling, kept as editable files rather than string constants.

    A language may keep its own copy of any template file in a subdirectory named after it, and
    that copy wins. Every language started from one shared design, but what a card should ask
    depends on how far along the learner is in that particular language: the same prompt that
    still teaches a beginner is a giveaway to someone who already reads the language. Without
    the override the only way to change one deck's cards would be to change the other's too.

    Styling stays shared and a language's own sheet is appended to it, so a variant adds rules
    rather than restating the whole design.
    """

    def __init__(
        self,
        *,
        directory: pathlib.Path,
        labels: typing.Optional[typing.Dict[str, str]] = None,
        variant: typing.Optional[str] = None,
        card_templates: typing.Sequence[str] = language_config.DEFAULT_CARD_TEMPLATES,
    ):
        self._directory = directory
        self._variant = directory / variant if variant else None
        self._labels = labels or {}
        self._card_templates = tuple(card_templates)

    @property
    def css(self) -> str:
        shared = (self._directory / STYLE_FILE).read_text(encoding='utf-8')
        own = self._variant / STYLE_FILE if self._variant is not None else None
        if own is not None and own.exists():
            return f'{shared}\n{own.read_text(encoding="utf-8")}'
        return shared

    def _file(self, *, name: str) -> pathlib.Path:
        if self._variant is not None and (self._variant / name).exists():
            return self._variant / name
        return self._directory / name

    @property
    def templates(self) -> typing.Dict[str, typing.Dict[str, str]]:
        return {
            name.capitalize(): {
                'Front': self._read(name=f'{name}.front.html'),
                'Back': self._read(name=f'{name}.back.html'),
            }
            for name in self._card_templates
        }

    def _read(self, *, name: str) -> str:
        text = self._file(name=name).read_text(encoding='utf-8')
        # Includes resolve first, so a label inside an included file is substituted too.
        text = INCLUDE_PATTERN.sub(lambda match: self._include(name=match.group(1)), text)
        return LABEL_PATTERN.sub(lambda match: self._labels.get(match.group(1), match.group(1)), text)

    def _include(self, *, name: str) -> str:
        return self._file(name=name).read_text(encoding='utf-8').strip()

    def as_create_payload(self) -> typing.List[dict]:
        return [{'Name': name, **sides} for name, sides in self.templates.items()]

    @property
    def referenced_fields(self) -> typing.List[str]:
        """Every field name the templates mention, in the order they first appear.

        Anki rejects a whole card template that names a field the note type does not have, so a
        design written for a language with a pivot cannot be pushed for one without unless the
        unused fields are declared anyway. Declared and empty costs nothing: an empty field
        renders as nothing and its conditional section is skipped.
        """
        names = []
        for sides in self.templates.values():
            for text in sides.values():
                for match in PLACEHOLDER_PATTERN.finditer(text):
                    name = match.group(1).strip().lstrip('#^/').split(':')[-1].strip()
                    if not name or name in BUILT_IN_PLACEHOLDERS or name in names:
                        continue
                    names.append(name)
        return names


class MediaLibrary:
    """Images and pronunciations extracted from the source app, ready to hand to Anki."""

    def __init__(self, *, directory: pathlib.Path, entries: typing.Dict[str, typing.Dict[str, str]]):
        self._directory = directory
        self._entries = entries

    @classmethod
    def load(cls, *, data_directory: pathlib.Path) -> 'MediaLibrary':
        manifest = data_directory / MEDIA_MANIFEST
        directory = data_directory / MEDIA_DIRECTORY
        if not manifest.exists():
            return cls(directory=directory, entries={})
        entries = {row['key']: row for row in language_config.TsvFile.read(manifest)}
        return cls(directory=directory, entries=entries)

    def __len__(self) -> int:
        return len(self._entries)

    def upload(self, *, client: 'AnkiConnect', keys: typing.Iterable[str]) -> int:
        """Hand each file to Anki by path, falling back to sending the bytes.

        A sandboxed Anki (flatpak, snap) cannot read arbitrary host paths, so the path form
        fails there. Probing once and switching is cheaper than base64-ing everything.
        """
        stored = 0
        by_path = True
        missing: typing.List[str] = []
        for key in keys:
            for filename in self.filenames_for(key=key):
                source = self._directory / filename
                # The manifest can outlive a file it names — a recode changes the extension, a
                # rejected picture is deleted. One absent file must not abort the whole push.
                if not source.exists():
                    missing.append(filename)
                    continue
                if by_path:
                    try:
                        client.invoke(
                            action='storeMediaFile',
                            filename=filename,
                            path=str(source.resolve()),
                            deleteExisting=False,
                        )
                        stored += 1
                        continue
                    except AnkiConnectError:
                        by_path = False
                        print("Anki cannot read local paths — sending file contents instead", file=sys.stderr)
                client.invoke(
                    action='storeMediaFile',
                    filename=filename,
                    data=base64.b64encode(source.read_bytes()).decode('ascii'),
                    deleteExisting=False,
                )
                stored += 1
        if missing:
            print(f"{len(missing)} media files named by the manifest are gone, e.g. {missing[0]}", file=sys.stderr)
        return stored

    def filenames_for(self, *, key: str) -> typing.List[str]:
        entry = self._entries.get(key, {})
        return [entry[column] for column in ('image', 'audio') if entry.get(column)]

    def fields_for(self, *, key: str) -> typing.Dict[str, str]:
        entry = self._entries.get(key, {})
        fields = {}
        if entry.get('image'):
            fields['Image'] = f'<img src="{entry["image"]}">'
        if entry.get('audio'):
            fields['Pronunciation'] = f'[sound:{entry["audio"]}]'
        return fields


class AnkiConnect:
    """Thin client over the add-on's single-endpoint JSON API."""

    def __init__(self, *, url: str = ANKI_CONNECT_URL):
        self._url = url

    def invoke(self, *, action: str, **params) -> typing.Any:
        payload = {'action': action, 'version': API_VERSION, 'params': params}
        request = urllib.request.Request(
            self._url,
            data=json.dumps(payload).encode('utf-8'),
            headers={'Content-Type': 'application/json'},
        )
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                body = json.load(response)
        except urllib.error.URLError as error:
            raise AnkiConnectError(
                "cannot reach AnkiConnect — open Anki and check the add-on is installed"
            ) from error
        if body.get('error') is not None:
            raise AnkiConnectError(f"{action} failed: {body['error']}")
        return body['result']

    def invoke_many(self, *, actions: typing.List[dict]) -> typing.List[typing.Any]:
        """Batch several actions into one round trip; each result is returned in order."""
        return self.invoke(action='multi', actions=actions)


class DeckBuilder:
    """Creates the deck and note type when they are missing, and leaves them alone otherwise."""

    def __init__(
        self,
        *,
        client: AnkiConnect,
        language: language_config.LanguageConfig,
        design: CardDesign,
    ):
        self._client = client
        self._language = language
        self._design = design

    def ensure_deck(self) -> None:
        name = self._language.deck_name
        if name not in self._client.invoke(action='deckNames'):
            self._client.invoke(action='createDeck', deck=name)
            print(f"created deck {name!r}")

    def ensure_subdecks(self, *, rows: typing.List[dict]) -> None:
        """Every subdeck a row can land in, created up front.

        Only the parent deck is guaranteed to exist otherwise, so a note or a card move
        aimed at a level subdeck would have nowhere to land on the very first push.
        """
        if not self._language.splits_deck_by_cefr:
            return
        existing = set(self._client.invoke(action='deckNames'))
        wanted = {self._language.deck_name_for(row=row) for row in rows}
        missing = sorted(wanted - existing)
        for name in missing:
            self._client.invoke(action='createDeck', deck=name)
        if missing:
            print(f"created subdecks {missing}")

    def ensure_note_type(self) -> None:
        name = self._language.note_type_name
        fields = self.field_names()
        if name in self._client.invoke(action='modelNames'):
            existing = self._client.invoke(action='modelFieldNames', modelName=name)
            missing = [field for field in fields if field not in existing]
            # Adding a field is safe — it appends an empty one to every note and touches nothing
            # that is already there — so a deck that predates a new field catches up by itself
            # rather than making the learner edit the note type by hand.
            for field in missing:
                self._client.invoke(action='modelFieldAdd', modelName=name, fieldName=field)
            if missing:
                print(f"added fields {missing} to {name!r}")
            return
        self._client.invoke(
            action='createModel',
            modelName=name,
            inOrderFields=fields,
            css=self._design.css,
            isCloze=False,
            cardTemplates=self._design.as_create_payload(),
        )
        print(f"created note type {name!r} with {len(fields)} fields")

    def update_design(self) -> None:
        """Push edited templates and styling onto an existing note type, so iterating on the
        card layout never means rebuilding the deck."""
        name = self._language.note_type_name
        self._add_new_templates(name=name)
        self._client.invoke(action='updateModelTemplates', model={'name': name, 'templates': self._design.templates})
        self._client.invoke(action='updateModelStyling', model={'name': name, 'css': self._design.css})
        print(f"refreshed templates and styling on {name!r}")

    def _add_new_templates(self, *, name: str) -> None:
        """A whole new kind of card has to be created before it can be updated.

        Updating templates only reaches the ones already on the note type, so a template added
        to the design after the deck was built is written nowhere and silently makes no cards.
        """
        existing = set(self._client.invoke(action='modelTemplates', modelName=name))
        missing = [key for key in self._design.templates if key not in existing]
        for key in missing:
            self._client.invoke(
                action='modelTemplateAdd',
                modelName=name,
                template={'Name': key, **self._design.templates[key]},
            )
        if missing:
            print(f"added card templates {missing} to {name!r}")

    def field_names(self) -> typing.List[str]:
        return self.note_type_fields(language=self._language, design=self._design)

    @staticmethod
    def note_type_fields(*, language: language_config.LanguageConfig, design: CardDesign) -> typing.List[str]:
        fields = [
            *language.note_fields().values(),
            *language.computed_fields,
            *language.media_fields,
        ]
        return fields + [name for name in design.referenced_fields if name not in fields]


class NotePusher:
    """Adds new notes and updates the ones already present, matching on the key field."""

    def __init__(
        self,
        *,
        client: AnkiConnect,
        language: language_config.LanguageConfig,
        media: typing.Optional[MediaLibrary] = None,
        replace_media: bool = False,
    ):
        self._client = client
        self._language = language
        self._media = media
        self._replace_media = replace_media

    def existing_notes(self) -> typing.Dict[str, dict]:
        """Read every note of this type once; per-note search would need query escaping.

        Field values come along so media already present on a note is never overwritten.
        """
        note_ids = self._client.invoke(action='findNotes', query=f'note:"{self._language.note_type_name}"')
        if not note_ids:
            return {}
        by_key = {}
        for info in self._client.invoke(action='notesInfo', notes=note_ids):
            key = info['fields'].get('Key', {}).get('value', '')
            if key:
                by_key[key] = {
                    'id': info['noteId'],
                    'fields': {name: value.get('value', '') for name, value in info['fields'].items()},
                    'cards': info['cards'],
                }
        return by_key

    def push(self, *, rows: typing.List[dict]) -> typing.Tuple[int, int]:
        existing = self.existing_notes()
        print(f"{len(existing)} notes already in the deck")

        if self._media is not None and len(self._media):
            stored = self._media.upload(client=self._client, keys=[row['key'] for row in rows])
            print(f"stored {stored} media files")

        to_add = [row for row in rows if row['key'] not in existing]
        to_update = [row for row in rows if row['key'] in existing]

        added, rejected = self._add(rows=to_add)
        updated = self._update(rows=to_update, existing=existing)

        print(f"added {added} | updated {updated} | rejected {len(rejected)}")
        for key in rejected[:10]:
            print(f"  rejected: {key}", file=sys.stderr)
        if len(rejected) > 10:
            print(f"  ... and {len(rejected) - 10} more", file=sys.stderr)

        if self._language.splits_deck_by_cefr:
            moved, subdeck_count = self._move_to_subdecks(rows=rows)
            print(f"moved {moved} cards into {subdeck_count} subdecks")

        return added, updated

    def retire(self, *, keys: typing.Sequence[str]) -> int:
        """Delete the notes whose cards were folded into another card, losing their review history.

        Anki holds a copy of the deck the table cannot reach, so a card folded away in the table is
        still asked in the deck until its note is removed. The removal is not reversible — the
        note's scheduling goes with it — which is why this is asked for rather than assumed.
        """
        existing = self.existing_notes()
        note_ids = [existing[key]['id'] for key in keys if key in existing]
        if not note_ids:
            return 0
        self._client.invoke(action='deleteNotes', notes=note_ids)
        return len(note_ids)

    def _move_to_subdecks(self, *, rows: typing.List[dict]) -> typing.Tuple[int, int]:
        """Move cards whose note already exists into the subdeck their row now names.

        Scheduling data — interval, ease, due date — lives on the card, not the deck, so
        changing a card's deck here never touches its review history; only where the card
        is filed changes.
        """
        current = self.existing_notes()
        card_ids = [card_id for info in current.values() for card_id in info['cards']]
        if not card_ids:
            return 0, 0
        current_deck = {
            info['cardId']: info['deckName']
            for info in self._client.invoke(action='cardsInfo', cards=card_ids)
        }

        moves: typing.Dict[str, typing.List[int]] = {}
        for row in rows:
            info = current.get(row['key'])
            if info is None:
                continue
            target_deck = self._language.deck_name_for(row=row)
            to_move = [card_id for card_id in info['cards'] if current_deck.get(card_id) != target_deck]
            if to_move:
                moves.setdefault(target_deck, []).extend(to_move)

        for deck, ids in moves.items():
            self._client.invoke(action='changeDeck', cards=ids, deck=deck)

        return sum(len(ids) for ids in moves.values()), len(moves)

    def _add(self, *, rows: typing.List[dict]) -> typing.Tuple[int, typing.List[str]]:
        added, rejected = 0, []
        for start in range(0, len(rows), ADD_BATCH_SIZE):
            batch = rows[start:start + ADD_BATCH_SIZE]
            notes = [self.build_note(row=row) for row in batch]
            results = self._client.invoke(action='addNotes', notes=notes)
            for row, note_id in zip(batch, results):
                if note_id is None:
                    rejected.append(row['key'])
                else:
                    added += 1
        return added, rejected

    def _update(self, *, rows: typing.List[dict], existing: typing.Dict[str, int]) -> int:
        updated = 0
        for start in range(0, len(rows), UPDATE_BATCH_SIZE):
            batch = rows[start:start + UPDATE_BATCH_SIZE]
            actions = [
                {
                    'action': 'updateNote',
                    'version': API_VERSION,
                    'params': {
                        'note': {
                            'id': existing[row['key']]['id'],
                            'fields': self.fields_with_media(
                                row=row,
                                present=existing[row['key']]['fields'],
                            ),
                            'tags': row.get('tags', '').split(),
                        },
                    },
                }
                for row in batch
            ]
            for result in self._client.invoke_many(actions=actions):
                # multi reports a per-action failure as a dict carrying an error message.
                if isinstance(result, dict) and result.get('error'):
                    print(f"  update failed: {result['error']}", file=sys.stderr)
                else:
                    updated += 1
        return updated

    def build_fields(self, *, row: dict) -> typing.Dict[str, str]:
        """Media fields are left out so a re-run never wipes audio or images already added."""
        return self.render_fields(row=row, language=self._language)

    @staticmethod
    def render_fields(*, row: dict, language: language_config.LanguageConfig) -> typing.Dict[str, str]:
        fields = {field: row.get(column, '') for column, field in language.note_fields().items()}
        fields['ExamplesHtml'] = NotePusher.render_examples(row=row, language=language)
        fields['ContextHtml'] = NotePusher.render_context(row=row, language=language)
        fields['ContextGlossHtml'] = NotePusher.render_context_gloss(row=row, language=language)
        fields['InflectionHtml'] = NotePusher.render_inflection(row=row, language=language)
        fields['Hint'] = NotePusher.render_hint(row=row)
        fields['SynonymsHtml'] = NotePusher.render_related(
            row=row, column='synonyms', language=language, keep=NotePusher.plain_synonyms,
        )
        fields['FigurativeHtml'] = NotePusher.render_related(
            row=row, column='synonyms', language=language, keep=NotePusher.figurative_synonyms,
        )
        fields['AntonymsHtml'] = NotePusher.render_related(row=row, column='antonyms', language=language)
        fields['OtherSensesHtml'] = NotePusher.render_other_senses(row=row, language=language)
        return fields

    def fields_with_media(self, *, row: dict, present: typing.Dict[str, str]) -> typing.Dict[str, str]:
        """Fill a media field only when the note has none, so audio added inside Anki survives.

        Under replace_media the manifest wins instead, including where it is now empty. That is
        what lets a picture judged wrong actually leave the deck: the protective rule cannot tell
        a deliberate removal from an accidental one, so removing has to be asked for explicitly.
        """
        fields = self.build_fields(row=row)
        if self._media is None:
            return fields
        found = self._media.fields_for(key=row['key'])
        for name in self._language.media_fields:
            value = found.get(name, '')
            if self._replace_media:
                fields[name] = value
            elif value and not present.get(name):
                fields[name] = value
        return fields

    @staticmethod
    def render_examples(*, row: dict, language: language_config.LanguageConfig) -> str:
        """Only the best few sentences reach the card; the rest stay in the fields."""
        suffixes = examples.ExampleSet.translation_suffixes(language=language)
        roles = dict(zip(suffixes, ('native', 'pivot')))
        chosen = examples.ExampleSet.rank(
            examples=examples.ExampleSet.read(row=row, language=language),
            suffixes=suffixes,
        )[:examples.CARD_EXAMPLE_LIMIT]
        blocks = []
        for example in chosen:
            # One line holding both glosses, so each sentence reads as a single unit rather
            # than as three stacked lines that all look like separate entries.
            glosses = ''.join(
                f'<span class="{roles[suffix]}">{example.translations[suffix]}</span>'
                for suffix in suffixes
                if example.translations.get(suffix)
            )
            block = f'<div class="target">{example.target}</div><div class="glosses">{glosses}</div>'
            blocks.append(f'<div class="example">{block}</div>')
        return ''.join(blocks)

    @staticmethod
    def _context_choice(
        *, row: dict, language: language_config.LanguageConfig,
    ) -> typing.Optional[typing.Tuple[examples.Example, str]]:
        """The sentence and target the sentence card is built on, or None when the row has none.

        A guard failing here must empty both the sentence and its gloss together, since a gloss
        with no sentence above it would be a card with no question.
        """
        target = row.get('context_target', '').strip()
        if not target:
            return None
        try:
            index = int(row.get('context_index', ''))
        except ValueError:
            return None
        chosen = examples.ExampleSet.read(row=row, language=language)
        if not 1 <= index <= len(chosen):
            return None
        example = chosen[index - 1]
        if target not in example.target:
            return None
        return example, target

    @staticmethod
    def render_context(*, row: dict, language: language_config.LanguageConfig) -> str:
        """The chosen example sentence, escaped for HTML, with the target word marked as the cue."""
        choice = NotePusher._context_choice(row=row, language=language)
        if choice is None:
            return ''
        example, target = choice
        escaped_sentence = html.escape(example.target)
        escaped_target = html.escape(target)
        cue_start = escaped_sentence.find(escaped_target)
        return (
            f'{escaped_sentence[:cue_start]}<span class="cue">{escaped_target}</span>'
            f'{escaped_sentence[cue_start + len(escaped_target):]}'
        )

    @staticmethod
    def render_context_gloss(*, row: dict, language: language_config.LanguageConfig) -> str:
        """The chosen sentence's translations, styled the same way render_examples does."""
        choice = NotePusher._context_choice(row=row, language=language)
        if choice is None:
            return ''
        example, _ = choice
        suffixes = examples.ExampleSet.translation_suffixes(language=language)
        roles = dict(zip(suffixes, ('native', 'pivot')))
        return ''.join(
            f'<span class="{roles[suffix]}">{example.translations[suffix]}</span>'
            for suffix in suffixes
            if example.translations.get(suffix)
        )

    @staticmethod
    def render_related(
        *,
        row: dict,
        column: str,
        language: language_config.LanguageConfig,
        keep: typing.Optional[typing.Callable[[dict], typing.Set[str]]] = None,
    ) -> str:
        """A related word beside what it means, since the bare foreign word teaches nothing.

        The gloss column is aligned word for word with the list it explains, so the two are read
        together by position; a word whose gloss is missing still shows, just unexplained.
        """
        words = [part.strip() for part in row.get(column, '').split(',') if part.strip()]
        glosses = [part.strip() for part in row.get(f'{column}_{language.native}', '').split(',')]
        wanted = keep(row) if keep is not None else {word.lower() for word in words}
        shown = []
        for index, word in enumerate(words):
            if word.lower() not in wanted:
                continue
            gloss = glosses[index].strip() if index < len(glosses) else ''
            meaning = f'<span class="gloss">{gloss}</span>' if gloss else ''
            shown.append(f'<span class="related"><span class="term">{word}</span>{meaning}</span>')
        return ''.join(shown)

    @staticmethod
    def render_other_senses(*, row: dict, language: language_config.LanguageConfig) -> str:
        """The headword's unlearned senses, each shown as its Russian meaning beside its example.

        The two columns are aligned by position the same way the synonym glosses are, so a sense
        missing its partner — an answer that only half-landed — is simply left out rather than
        shown broken.
        """
        target_column, native_column = language.other_senses_columns
        examples_shown = [part for part in row.get(target_column, '').split(senses.SENSE_SEPARATOR) if part]
        meanings = [part for part in row.get(native_column, '').split(senses.SENSE_SEPARATOR) if part]
        shown = [
            f'<span class="related"><span class="term">{meaning}</span><span class="gloss">{example}</span></span>'
            for meaning, example in zip(meanings, examples_shown)
        ]
        return ''.join(shown)

    @staticmethod
    def _listed(*, row: dict, column: str) -> typing.Set[str]:
        return {part.strip().lower() for part in row.get(column, '').split(',') if part.strip()}

    @classmethod
    def figurative_synonyms(cls, row: dict) -> typing.Set[str]:
        return cls._listed(row=row, column='figurative_synonyms')

    @classmethod
    def plain_synonyms(cls, row: dict) -> typing.Set[str]:
        """What is left once the figurative ones have their own heading and the wrong ones go."""
        excluded = cls.figurative_synonyms(row) | cls._listed(row=row, column='rejected_synonyms')
        return cls._listed(row=row, column='synonyms') - excluded

    @staticmethod
    def render_plain_synonyms(*, row: dict) -> str:
        """The synonyms that stand in for the word literally, and belong on the card at all.

        Two groups come out: the figurative ones, which get their own heading and so must not
        appear here as well, and the ones judged not to be synonyms, which get no heading and
        simply do not reach the card. Both are still in the table, so a bad call is reversible.
        """
        shown_elsewhere = {
            part.strip().lower()
            for column in ('figurative_synonyms', 'rejected_synonyms')
            for part in row.get(column, '').split(',')
            if part.strip()
        }
        plain = [
            part.strip()
            for part in row.get('synonyms', '').split(',')
            if part.strip() and part.strip().lower() not in shown_elsewhere
        ]
        return ', '.join(plain)

    @staticmethod
    def render_hint(*, row: dict) -> str:
        """The headword with everything but its first letters masked, one mark per letter.

        Producing a word from its translation with nothing to go on is where a session stalls:
        the answer is either there or it is not, and a blank teaches nothing. A first letter and
        an exact length turn that blank back into recall, which is the point of the card — so it
        stays behind a tap rather than being shown, or it stops being recall at all.
        """
        masked = ' '.join(
            part[0] + HINT_MASK_CHARACTER * (len(part) - 1)
            for part in row.get('word', '').split()
            if part
        )
        return masked

    @staticmethod
    def render_inflection(*, row: dict, language: language_config.LanguageConfig) -> str:
        """A row of pronoun-labelled forms, then the other tenses as one line of stems."""
        raw = row.get('inflection', '')
        if not raw:
            return ''
        forms = json.loads(raw)
        settings = language.inflection
        pronouns = settings.get('pronouns', ())
        labels = settings.get('row_labels', {})
        blocks = []
        for label, values in forms.items():
            shown = labels.get(label, label)
            if len(values) > 1 and len(values) == len(pronouns):
                cells = ''.join(
                    f'<div class="slot"><span class="pronoun">{pronoun}</span>'
                    f'<span class="form">{value}</span></div>'
                    for pronoun, value in zip(pronouns, values)
                )
                blocks.append(f'<div class="paradigm"><div class="tense">{shown}</div>{cells}</div>')
            else:
                joined = ', '.join(values)
                blocks.append(
                    f'<div class="principal"><span class="tense">{shown}</span>'
                    f'<span class="form">{joined}</span></div>'
                )
        return ''.join(blocks)

    def build_note(self, *, row: dict) -> dict:
        fields = self.build_fields(row=row)
        if self._media is not None:
            fields.update(self._media.fields_for(key=row['key']))
        return {
            'deckName': self._language.deck_name_for(row=row),
            'modelName': self._language.note_type_name,
            'fields': fields,
            'tags': row.get('tags', '').split(),
            'options': {'allowDuplicate': False},
        }


def main_for(
    *,
    language: language_config.LanguageConfig,
    root: pathlib.Path,
    replace_media: bool = False,
    design_only: bool = False,
    limit: typing.Optional[int] = None,
    url: str = ANKI_CONNECT_URL,
    input_path: typing.Optional[pathlib.Path] = None,
    retire_folded: bool = False,
) -> typing.Tuple[int, int]:
    input_path = input_path or language.data_directory(root=root) / 'cards.tsv'

    rows = [] if design_only else language_config.TsvFile.read(input_path)
    # A card folded into another keeps its row so the fold stays readable, but it is no longer a
    # card: pushing it would put the question back that the fold was meant to stop asking.
    folded_keys = [row['key'] for row in rows if row.get(MERGED_COLUMN, '').strip()]
    if folded_keys:
        rows = [row for row in rows if not row.get(MERGED_COLUMN, '').strip()]
        print(f"skipping {len(folded_keys)} cards folded into another")
    if limit is not None:
        rows = rows[:limit]

    client = AnkiConnect(url=url)
    design = CardDesign(
        directory=root / TEMPLATE_DIRECTORY,
        labels=language.labels,
        variant=language.target,
        card_templates=language.card_templates,
    )
    builder = DeckBuilder(client=client, language=language, design=design)
    builder.ensure_deck()
    builder.ensure_subdecks(rows=rows)
    builder.ensure_note_type()
    builder.update_design()
    if design_only:
        return 0, 0
    media = MediaLibrary.load(data_directory=language.data_directory(root=root))
    print(f"{len(media)} words have media extracted from the source app")
    pusher = NotePusher(client=client, language=language, media=media, replace_media=replace_media)
    if retire_folded and folded_keys:
        print(f"deleted {pusher.retire(keys=folded_keys)} notes folded into another card")
    return pusher.push(rows=rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', nargs='?', type=pathlib.Path, default=None)
    parser.add_argument('--limit', type=int, default=None, help="Push only the first N rows")
    parser.add_argument('--url', default=ANKI_CONNECT_URL, help="AnkiConnect endpoint")
    parser.add_argument(
        '--replace-media',
        action='store_true',
        help="Let the manifest overwrite image and audio fields, including clearing them",
    )
    parser.add_argument(
        '--design-only',
        action='store_true',
        help="Only refresh card templates and styling, do not touch notes",
    )
    parser.add_argument(
        '--retire-folded',
        action='store_true',
        help="Delete the notes of cards folded into another, losing their review history",
    )
    language_config.add_language_arguments(parser)
    arguments = parser.parse_args()

    root = language_config.PROJECT_ROOT
    language = language_config.language_from(arguments, root=root)
    main_for(
        language=language,
        root=root,
        replace_media=arguments.replace_media,
        design_only=arguments.design_only,
        limit=arguments.limit,
        url=arguments.url,
        input_path=arguments.input,
        retire_folded=arguments.retire_folded,
    )


if __name__ == '__main__':
    main()
