"""The one place that knows how language roles map onto concrete column names.

Every stage works in terms of roles — the language being learned, the learner's own
language, an optional pivot language — so no stage mentions a specific language. The
concrete names live in languages/<code>.json and are rendered here.
"""

import json
import pathlib
import typing

# Every stage resolves templates, per-language config and generated data against this, so the
# package can be run from anywhere without each stage rediscovering where the project lives.
PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent

CONFIG_DIRECTORY = 'languages'

# Almost everything a language needs is derivable from its code. Only the facts below
# resist derivation: its English name, its ISO 639-3 code (which sentence corpora use),
# and its articles. A file under languages/ overrides any of it when a language needs
# something special — a better dictionary than Wiktionary, say.
LANGUAGE_FACTS = {
    'es': {'name': 'Spanish', 'iso3': 'spa', 'articles': ('el', 'la', 'los', 'las', 'un', 'una', 'unos', 'unas')},
    'en': {'name': 'English', 'iso3': 'eng', 'articles': ('the', 'a', 'an')},
    'ru': {'name': 'Russian', 'iso3': 'rus'},
    'sr': {'name': 'Serbian', 'iso3': 'srp'},
    'fr': {'name': 'French', 'iso3': 'fra', 'articles': ('le', 'la', 'les', 'un', 'une', 'des')},
    'de': {'name': 'German', 'iso3': 'deu', 'articles': ('der', 'die', 'das', 'ein', 'eine')},
    'it': {'name': 'Italian', 'iso3': 'ita', 'articles': ('il', 'lo', 'la', 'i', 'gli', 'le', 'un', 'uno', 'una')},
    'pt': {'name': 'Portuguese', 'iso3': 'por', 'articles': ('o', 'a', 'os', 'as', 'um', 'uma')},
    'nl': {'name': 'Dutch', 'iso3': 'nld', 'articles': ('de', 'het', 'een')},
    'pl': {'name': 'Polish', 'iso3': 'pol'},
    'cs': {'name': 'Czech', 'iso3': 'ces'},
    'tr': {'name': 'Turkish', 'iso3': 'tur'},
    'sv': {'name': 'Swedish', 'iso3': 'swe', 'articles': ('en', 'ett')},
    'el': {'name': 'Greek', 'iso3': 'ell'},
    'he': {'name': 'Hebrew', 'iso3': 'heb'},
    'ar': {'name': 'Arabic', 'iso3': 'ara'},
    'ja': {'name': 'Japanese', 'iso3': 'jpn'},
    'ko': {'name': 'Korean', 'iso3': 'kor'},
    'zh': {'name': 'Chinese', 'iso3': 'zho'},
    'ka': {'name': 'Georgian', 'iso3': 'kat'},
    'hy': {'name': 'Armenian', 'iso3': 'hye'},
}

# Card headings are shown in the learner's own language.
SECTION_LABELS = {
    'ru': {'examples': 'Примеры', 'synonyms': 'Синонимы', 'antonyms': 'Антонимы', 'forms': 'Формы', 'figurative': 'В переносном смысле',
           'senses': 'Другие значения', 'check_native': 'проверить перевод', 'guess': 'Какое это слово?',
           'read_from_context': 'Что значит выделенное слово?',
           'new': 'Новая', 'learning': 'Учится', 'young': 'Молодая', 'mature': 'Зрелая', 'days': 'д'},
    'en': {'examples': 'Examples', 'synonyms': 'Synonyms', 'antonyms': 'Antonyms', 'forms': 'Forms', 'figurative': 'Figuratively',
           'senses': 'Other meanings', 'check_native': 'check the translation', 'guess': 'Which word is it?',
           'read_from_context': 'What does the marked word mean?',
           'new': 'New', 'learning': 'Learning', 'young': 'Young', 'mature': 'Mature', 'days': 'd'},
}
DEFAULT_LABEL_LANGUAGE = 'en'
# English is the useful pivot for image search and extra translations, unless it is the
# language being learned.
DEFAULT_PIVOT = 'en'
DEFAULT_NATIVE = 'ru'

# Columns that carry no language and are spelled the same in every deck.
NEUTRAL_COLUMNS = (
    'key',
    'word',
    'article',
    'part_of_speech',
    'gender',
    'number',
    'ipa',
)
PROVENANCE_COLUMNS = (
    'level_source',
    'relations_source',
    'frequency_band',
    'categories',
    'status',
    'has_image',
    'image_rejected',
    'definition_source',
    'examples_source',
    # The key of the card this one was folded into, where two cards taught one meaning. The row
    # stays so a bad fold can be read back; the deck skips it.
    'merged_into',
)
# Every media provider writes this one manifest, filling only the cells it found empty.
MEDIA_MANIFEST_FILENAME = 'media.tsv'
MEDIA_MANIFEST_COLUMNS = (
    'key',
    'image',
    'audio',
    'image_source',
    'image_license',
    'image_attribution',
    'audio_source',
)

JUDGEMENT_COLUMNS = (
    'synonyms',
    'antonyms',
    'cefr',
    'is_object',
    'context_index',
    'context_target',
)

# The only deck_split value this pipeline understands. Anything else, including absence,
# means one flat deck.
DECK_SPLIT_CEFR = 'cefr'
# Sorts after every CEFR level, so a row the judgement stage left unleveled lands in a
# subdeck of its own at the end of the study order rather than in the shared parent deck.
UNLEVELED_SUBDECK_NAME = 'Unleveled'

# What a language gets unless its own config names a different set: recognise the foreign word,
# produce it from the native prompt, and read it inside a sentence.
DEFAULT_CARD_TEMPLATES = ('recognition', 'recall', 'context')


class LanguageConfig:
    """Names and column suffixes for one target language."""

    def __init__(self, *, data: dict):
        self.code: str = data['code']
        self.target_name: str = data['target']['name']
        self.native_name: str = data['native']['name']
        self.target: str = data['target']['suffix']
        self.native: str = data['native']['suffix']
        self.pivot: typing.Optional[str] = data['pivot']['suffix'] if data.get('pivot') else None
        self.has_articles: bool = data.get('has_articles', False)
        self.has_grammatical_gender: bool = data.get('has_grammatical_gender', False)
        self.wiktionary_host: str = data.get('wiktionary_host', '')
        # An edition names a language section in its own language, not in English, so the deck's
        # own name for the language is the wrong string to look for in anything but the English one.
        self.wiktionary_section: str = data.get('wiktionary_section', self.target_name)
        self.corpus_language_code: str = data.get('corpus_language_code', '')
        self._labels: typing.Dict[str, str] = data.get('labels', {})
        self.articles: typing.Tuple[str, ...] = tuple(data.get('articles', ()))
        self.dictionary: str = data.get('dictionary', 'wiktionary')
        self.media: typing.Dict[str, typing.Any] = data.get('media', {})
        self.inflection: typing.Dict[str, typing.Any] = data.get('inflection', {})
        self.graded_lexicon: typing.List[typing.Dict[str, typing.Any]] = data.get('graded_lexicon', [])
        # Which kinds of card a word gets. A deck the learner already reads has no use for the
        # ones that ask them to produce a word from its meaning, and a beginner's deck lives on
        # them, so the set belongs to the language rather than to the templates.
        self.card_templates: typing.Tuple[str, ...] = tuple(
            data.get('card_templates', DEFAULT_CARD_TEMPLATES),
        )
        # Which card stands down for which, when both could be built from the same word. Two
        # cards asking what a word means are one question asked twice, and the one with more to
        # go on is the one worth answering — but only a word that actually got the richer card
        # can spare the plainer one, so this is settled per word rather than per deck.
        self.superseded_cards: typing.Dict[str, str] = {
            str(winner).lower(): str(loser).lower()
            for winner, loser in data.get('superseded_cards', {}).items()
        }
        self.wikipedia_host: str = data.get('wikipedia_host', '')
        self.deck_split: typing.Optional[str] = data.get('deck_split')
        self.mastered_levels: typing.Tuple[str, ...] = tuple(
            str(level).strip().upper() for level in data.get('mastered_levels', ())
        )
        # A separate top-level deck for finished levels, so studying the main deck never reaches them.
        self.mastered_deck: typing.Optional[str] = data.get('mastered_deck')
        self._deck: str = data.get('deck', self.target_name)

    def name_of(self, *, suffix: str) -> str:
        """Display name for a column suffix, for prompts that address languages by role."""
        return LANGUAGE_FACTS.get(suffix, {}).get('name', suffix)

    @classmethod
    def for_languages(cls, *, target: str, native: str, root: pathlib.Path) -> 'LanguageConfig':
        """Build a working config from nothing but the two language codes."""
        data = cls.derive(target=target, native=native)
        override_path = root / CONFIG_DIRECTORY / f'{target}.json'
        if override_path.exists():
            data = cls.merge(base=data, override=json.loads(override_path.read_text(encoding='utf-8')))
        return cls(data=data)

    @classmethod
    def load(cls, *, code: str, root: pathlib.Path) -> 'LanguageConfig':
        override_path = root / CONFIG_DIRECTORY / f'{code}.json'
        override = json.loads(override_path.read_text(encoding='utf-8')) if override_path.exists() else {}
        native = override.get('native', {}).get('suffix', DEFAULT_NATIVE)
        return cls.for_languages(target=code, native=native, root=root)

    @staticmethod
    def derive(*, target: str, native: str) -> dict:
        target_facts = LANGUAGE_FACTS.get(target, {})
        native_facts = LANGUAGE_FACTS.get(native, {})
        pivot = None if target == DEFAULT_PIVOT or native == DEFAULT_PIVOT else DEFAULT_PIVOT
        return {
            'code': target,
            'target': {'name': target_facts.get('name', target), 'suffix': target},
            'native': {'name': native_facts.get('name', native), 'suffix': native},
            'pivot': {'name': LANGUAGE_FACTS[DEFAULT_PIVOT]['name'], 'suffix': pivot} if pivot else None,
            'articles': list(target_facts.get('articles', ())),
            'has_articles': bool(target_facts.get('articles')),
            'wiktionary_host': f'{target}.wiktionary.org',
            'wikipedia_host': f'{target}.wikipedia.org',
            'corpus_language_code': target_facts.get('iso3', target),
            'dictionary': 'wiktionary',
            'labels': SECTION_LABELS.get(native, SECTION_LABELS[DEFAULT_LABEL_LANGUAGE]),
            'media': {
                'audio_providers': ['wiktionary'],
                'image_providers': ['openverse', 'wikipedia'],
                # Commons puts every language's recording on one page; these mark ours.
                'audio_filename_markers': [f'({target_facts.get("iso3", target)})', f'{target.capitalize()}-'],
            },
        }

    @staticmethod
    def merge(*, base: dict, override: dict) -> dict:
        merged = dict(base)
        for key, value in override.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = {**merged[key], **value}
            else:
                merged[key] = value
        return merged

    def data_directory(self, *, root: pathlib.Path) -> pathlib.Path:
        return root / 'data' / self.target

    @property
    def deck_name(self) -> str:
        return self._deck

    @property
    def splits_deck_by_cefr(self) -> bool:
        """Whether a row belongs in a level subdeck rather than the flat deck.

        Anki gathers new cards subdeck by subdeck in name order, and CEFR level names
        already sort into study order (A1, A2, B1, B2, C1, C2), so this gets the desired
        progression for free without touching card due positions.
        """
        return self.deck_split == DECK_SPLIT_CEFR

    def deck_name_for(self, *, row: dict) -> str:
        """Where a row's note belongs: its own subdeck if it names one, else the flat deck or its CEFR subdeck."""
        subdeck = row.get('subdeck', '').strip()
        if subdeck:
            return f'{self.deck_name}::{subdeck}'
        if not self.splits_deck_by_cefr:
            return self.deck_name
        level = row.get('cefr', '').strip() or UNLEVELED_SUBDECK_NAME
        return f'{self.parent_deck_for(level=level)}::{level}'

    def parent_deck_for(self, *, level: str) -> str:
        if self.mastered_deck is not None and level.upper() in self.mastered_levels:
            return self.mastered_deck
        return self.deck_name

    @property
    def note_type_name(self) -> str:
        return f'{self.target_name} Vocabulary'

    @property
    def translation_columns(self) -> typing.Tuple[str, ...]:
        columns = [f'translations_{self.native}']
        if self.pivot is not None:
            columns.append(f'translations_{self.pivot}')
        return tuple(columns)

    @property
    def example_columns(self) -> typing.Tuple[str, ...]:
        suffixes = [self.target, self.native]
        if self.pivot is not None:
            suffixes.append(self.pivot)
        return tuple(f'examples_{suffix}' for suffix in suffixes)

    @property
    def dictionary_definition_column(self) -> str:
        return self.definition_full_column

    @property
    def translations_native_column(self) -> str:
        return f'translations_{self.native}'

    @property
    def translations_pivot_column(self) -> typing.Optional[str]:
        return f'translations_{self.pivot}' if self.pivot is not None else None

    def examples_column(self, *, suffix: str) -> str:
        return f'examples_{suffix}'

    @property
    def derived_columns(self) -> typing.Tuple[str, ...]:
        """What a source can supply directly, before any lookup."""
        return self.extracted_columns[:-len(JUDGEMENT_COLUMNS) - 1]

    @property
    def enriched_columns(self) -> typing.Tuple[str, ...]:
        """Left empty by an entry point; filled by the dictionary and model stages."""
        return (self.definition_column, *JUDGEMENT_COLUMNS)

    @property
    def columns(self) -> typing.Tuple[str, ...]:
        """What an entry point writes: the extracted columns, gaps left for later stages."""
        return self.extracted_columns

    @property
    def definition_column(self) -> str:
        return f'definition_{self.target}'

    @property
    def definition_translation_columns(self) -> typing.Tuple[str, ...]:
        """The definition restated in the reader's own languages, beside the original."""
        suffixes = [self.native]
        if self.pivot is not None:
            suffixes.append(self.pivot)
        return tuple(f'definition_{suffix}' for suffix in suffixes)

    @property
    def related_translation_columns(self) -> typing.Tuple[str, ...]:
        """Glosses for the synonym and antonym lists, aligned word for word with them."""
        return (f'synonyms_{self.native}', f'antonyms_{self.native}')

    @property
    def other_senses_columns(self) -> typing.Tuple[str, ...]:
        """The headword's unlearned senses: an example in the target language, glossed in the reader's own."""
        return (f'other_senses_{self.target}', f'other_senses_{self.native}')

    @property
    def definition_full_column(self) -> str:
        """The dictionary's own wording, kept so a bad simplification can be spotted."""
        return f'definition_full_{self.target}'

    @property
    def extracted_columns(self) -> typing.Tuple[str, ...]:
        """What the source-list export produces; later stages only fill cells."""
        return (
            *NEUTRAL_COLUMNS[:6],
            *self.translation_columns,
            'ipa',
            *self.example_columns,
            'frequency_band',
            'categories',
            'status',
            'has_image',
            self.definition_column,
            *JUDGEMENT_COLUMNS,
        )

    @property
    def card_columns(self) -> typing.Tuple[str, ...]:
        """The finished table handed to the Anki import."""
        return (
            *NEUTRAL_COLUMNS[:6],
            *self.translation_columns,
            'ipa',
            self.definition_column,
            *self.definition_translation_columns,
            self.definition_full_column,
            *self.example_columns,
            'dictionary_examples',
            'inflection',
            'figurative_synonyms',
            'rejected_synonyms',
            *self.related_translation_columns,
            *self.other_senses_columns,
            *JUDGEMENT_COLUMNS,
            *PROVENANCE_COLUMNS,
            'tags',
        )

    def note_fields(self) -> typing.Dict[str, str]:
        """Column to Anki field name. Insertion order is the note type's field order.

        The key field comes first because Anki treats a note type's first field as its
        deduplication key.
        """
        fields = {
            'key': 'Key',
            'word': 'Word',
            'article': 'Article',
            'part_of_speech': 'PartOfSpeech',
            'gender': 'Gender',
            'number': 'Number',
            f'translations_{self.native}': 'TranslationNative',
        }
        if self.pivot is not None:
            fields[f'translations_{self.pivot}'] = 'TranslationPivot'
        fields['ipa'] = 'IPA'
        fields[self.definition_column] = 'Definition'
        for column, field in zip(
            self.definition_translation_columns, ('DefinitionNative', 'DefinitionPivot'),
        ):
            fields[column] = field
        fields[self.definition_full_column] = 'DefinitionFull'
        fields[f'examples_{self.target}'] = 'ExamplesTarget'
        fields[f'examples_{self.native}'] = 'ExamplesNative'
        if self.pivot is not None:
            fields[f'examples_{self.pivot}'] = 'ExamplesPivot'
        fields['dictionary_examples'] = 'DictionaryExamples'
        fields['synonyms'] = 'Synonyms'
        fields['figurative_synonyms'] = 'FigurativeSynonyms'
        fields['antonyms'] = 'Antonyms'
        fields['cefr'] = 'CEFR'
        fields['frequency_band'] = 'FrequencyBand'
        fields['categories'] = 'Categories'
        return fields

    @property
    def labels(self) -> typing.Dict[str, str]:
        """Section headings shown on the card, in the learner's own language."""
        return self._labels

    @property
    def computed_fields(self) -> typing.Tuple[str, ...]:
        """Rendered at push time. Anki templates cannot interleave two fields line by line,
        so the aligned example columns are woven into one block here instead."""
        return (
            'ExamplesHtml', 'InflectionHtml', 'SynonymsHtml', 'FigurativeHtml', 'AntonymsHtml',
            'OtherSensesHtml', 'Hint', 'ContextHtml', 'ContextGlossHtml',
        )

    @property
    def media_fields(self) -> typing.Tuple[str, ...]:
        """Filled inside Anki, not by this pipeline: audio by HyperTTS, image separately."""
        return ('Pronunciation', 'Image')


def add_language_arguments(parser) -> None:
    """The only language input the pipeline takes: what you are learning, and your own."""
    parser.add_argument('--target', required=True, help="Language being learned, e.g. es")
    parser.add_argument('--native', default=DEFAULT_NATIVE, help="Your own language")


def language_from(arguments, *, root: pathlib.Path) -> 'LanguageConfig':
    return LanguageConfig.for_languages(target=arguments.target, native=arguments.native, root=root)


class TsvFile:
    """Minimal tab-separated reader and writer; cells never contain tabs or newlines."""

    @staticmethod
    def read(path: pathlib.Path) -> typing.List[typing.Dict[str, str]]:
        lines = path.read_text(encoding='utf-8').splitlines()
        if not lines:
            raise ValueError(f"{path} is empty — expected a header row")
        header = lines[0].split('\t')
        return [dict(zip(header, line.split('\t'))) for line in lines[1:] if line]

    @staticmethod
    def write(path: pathlib.Path, *, rows: typing.Iterable[dict], columns: typing.Sequence[str]) -> None:
        """Write the named columns, and keep any others the rows happen to carry.

        A stage holds the column list it started with, so one that runs for an hour will write
        the table back in a shape that predates whatever was added meanwhile — and a column not
        named here would simply vanish, taking the work that filled it. Carrying the extras
        through costs a set difference and makes that loss impossible.
        """
        rows = list(rows)
        extra = [
            column for column in dict.fromkeys(name for row in rows for name in row)
            if column not in columns
        ]
        header = [*columns, *extra]
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('w', encoding='utf-8') as stream:
            stream.write('\t'.join(header) + '\n')
            for row in rows:
                stream.write('\t'.join(row.get(column, '') for column in header) + '\n')
