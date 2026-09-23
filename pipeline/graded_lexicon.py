"""Take the words a published graded lexicon says a learner meets, and the level it says it.

Most vocabulary lists carry no level, which is why the model is asked to judge one. Where a
graded lexicon exists it is better than that judgement for new words — it is measured rather
than guessed — and it also supplies the words themselves, which a frequency list cannot: what
a learner meets at A1 is not the same question as what is common.

A language can draw on several such lexicons at once, consulted in the order they are listed;
the first that answers a word wins, and its name travels with the answer. They come in two
shapes that earn very different trust.

A vocabulary profile is assigned by people, one level per headword and part of speech. It is a
verdict: when it has an answer for a card it is taken outright, free to raise a level as readily
as lower one, because the people who wrote it judged the word rather than merely counted its
appearances.

A count-derived lexicon instead records where a word *appears* in graded material, not where it
is *taught*. A word can turn up once in an A1 reader as incidental vocabulary, and reading that
as "this is an A1 word" places it far below where any course presents it. Requiring the word in
more than one text at that level removes the single strays; nothing removes the rest, so the
level it gives is a floor rather than a verdict. It earns a cautious rule to match: it may only
lower a card's level, only when the two disagree by more than a step, and only when the word is
also common enough that the early appearance looks like where a learner actually meets it rather
than a themed rarity.

It is also keyed on the headword, and cards are keyed on the sense. One spelling with two
unrelated meanings gets one level here and two cards in the deck, and no threshold reconciles
that — another reason a count-derived answer may only make the case for lowering a level, never
overwrite it outright the way a profile does.
"""

import argparse
import collections
import csv
import enum
import pathlib
import typing
import urllib.request

from . import language_config
from . import wordlist

WORDS_FILENAME = 'words.tsv'
CARDS_FILENAME = 'cards.tsv'
USER_AGENT = 'my-anki/1.0 (personal deck build)'
REQUEST_TIMEOUT_SECONDS = 60

# The order every level comparison in this module works in, lowered for case-insensitive use.
CEFR_LEVELS = ('a1', 'a2', 'b1', 'b2', 'c1', 'c2')

# A single appearance is not evidence a level teaches the word; two texts is.
DEFAULT_DOCUMENT_THRESHOLD = 2.0
# Multi-word entries are phrases the deck keeps as their own headwords, not vocabulary items.
PHRASE_MARKER = '_'


class GradedLexiconFormat(enum.StrEnum):
    """The two shapes a configured source can take."""

    CEFRLEX = 'cefrlex'
    PROFILE = 'profile'


class GradedLexiconSource(typing.Protocol):
    """What every source, whatever its format, offers the lexicon that aggregates them."""

    name: str
    format: GradedLexiconFormat

    def __len__(self) -> int: ...

    def level_of(self, *, word: str, part_of_speech: str = '') -> typing.Optional[str]: ...

    def part_of_speech_of(self, *, word: str) -> typing.Optional[str]: ...

    def words_at(self, *, levels: typing.AbstractSet[str]) -> typing.Dict[str, str]: ...


class CefrlexSource:
    """Headword to CEFR level, derived from per-level document counts."""

    format = GradedLexiconFormat.CEFRLEX

    def __init__(self, *, name: str, levels: typing.Dict[str, str]):
        self.name = name
        self._levels = levels

    def __len__(self) -> int:
        return len(self._levels)

    def level_of(self, *, word: str, part_of_speech: str = '') -> typing.Optional[str]:
        return self._levels.get(word.strip().lower())

    def part_of_speech_of(self, *, word: str) -> typing.Optional[str]:
        return None

    def words_at(self, *, levels: typing.AbstractSet[str]) -> typing.Dict[str, str]:
        return {word: level for word, level in self._levels.items() if level in levels}

    @classmethod
    def load(
        cls, *, name: str, path: pathlib.Path, order: typing.Sequence[str], threshold: float,
    ) -> 'CefrlexSource':
        return cls(name=name, levels=cls._levels_from(path=path, order=order, threshold=threshold))

    @staticmethod
    def _levels_from(*, path: pathlib.Path, order: typing.Sequence[str], threshold: float) -> typing.Dict[str, str]:
        lines = path.read_text(encoding='utf-8').splitlines()
        header = [cell.strip('"') for cell in lines[0].split('\t')]
        columns = {level: header.index(f'nb_doc@{level}') for level in order}
        levels: typing.Dict[str, str] = {}
        for line in lines[1:]:
            cells = [cell.strip('"') for cell in line.split('\t')]
            word = cells[0].lower()
            if PHRASE_MARKER in word:
                continue
            for index, level in enumerate(order):
                if float(cells[columns[level]] or 0) < threshold:
                    continue
                # One spelling can hold several parts of speech; a learner meets it at the
                # earliest of them, so the earliest level is the one that counts.
                known = levels.get(word)
                if known is None or index < order.index(known):
                    levels[word] = level
                break
        return levels


class ProfileSource:
    """Headword and part of speech to CEFR level, assigned by the profile's compilers."""

    format = GradedLexiconFormat.PROFILE

    def __init__(self, *, name: str, entries: typing.Dict[str, typing.Dict[str, str]]):
        self.name = name
        self._entries = entries

    def __len__(self) -> int:
        return len(self._entries)

    def level_of(self, *, word: str, part_of_speech: str = '') -> typing.Optional[str]:
        by_part = self._entries.get(word.strip().lower())
        if by_part is None:
            return None
        if part_of_speech in by_part:
            return by_part[part_of_speech]
        # No card yet knows the part of speech, or the profile never recorded that one; a
        # learner meets the word at the earliest level it holds for any part of speech.
        return min(by_part.values(), key=CEFR_LEVELS.index)

    def part_of_speech_of(self, *, word: str) -> typing.Optional[str]:
        """The part of speech the word's earliest level is stated for, so level and part of speech agree."""
        by_part = {part: level for part, level in self._entries.get(word.strip().lower(), {}).items() if part}
        if not by_part:
            return None
        return min(by_part, key=lambda part: CEFR_LEVELS.index(by_part[part]))

    def words_at(self, *, levels: typing.AbstractSet[str]) -> typing.Dict[str, str]:
        found = {}
        for word in self._entries:
            level = self.level_of(word=word)
            if level in levels:
                found[word] = level
        return found

    @classmethod
    def load(cls, *, name: str, path: pathlib.Path) -> 'ProfileSource':
        entries: typing.Dict[str, typing.Dict[str, str]] = {}
        with path.open(encoding='utf-8-sig', newline='') as stream:
            for row in csv.DictReader(stream):
                level = (row.get('CEFR') or '').strip().lower()
                if level not in CEFR_LEVELS:
                    continue
                part_of_speech = (row.get('pos') or '').strip().lower()
                # A headword cell can hold several spelling variants side by side, each its own entry.
                for variant in (row.get('headword') or '').split('/'):
                    variant = variant.strip().lower()
                    if variant:
                        entries.setdefault(variant, {})[part_of_speech] = level
        return cls(name=name, entries=entries)


class GradedLevel(typing.NamedTuple):
    """A level and the source whose answer it is."""

    level: str
    source: str


class GradedLexicon:
    """An ordered list of graded-lexicon sources, consulted first-answer-wins."""

    def __init__(self, *, sources: typing.Sequence[GradedLexiconSource]):
        self.sources = list(sources)

    def __len__(self) -> int:
        return sum(len(source) for source in self.sources)

    def level_of(self, *, word: str, part_of_speech: str = '') -> typing.Optional[GradedLevel]:
        for source in self.sources:
            level = source.level_of(word=word, part_of_speech=part_of_speech)
            if level is not None:
                return GradedLevel(level=level, source=source.name)
        return None

    def part_of_speech_of(self, *, word: str) -> typing.Optional[str]:
        for source in self.sources:
            part_of_speech = source.part_of_speech_of(word=word)
            if part_of_speech is not None:
                return part_of_speech
        return None

    def words_at(self, *, levels: typing.Sequence[str]) -> typing.Dict[str, GradedLevel]:
        wanted = {level.lower() for level in levels}
        found: typing.Dict[str, GradedLevel] = {}
        for source in self.sources:
            for word, level in source.words_at(levels=wanted).items():
                found.setdefault(word, GradedLevel(level=level, source=source.name))
        return found

    @classmethod
    def load(
        cls,
        *,
        language: language_config.LanguageConfig,
        data_directory: pathlib.Path,
        threshold: float = DEFAULT_DOCUMENT_THRESHOLD,
    ) -> 'GradedLexicon':
        sources: typing.List[GradedLexiconSource] = []
        for settings in language.graded_lexicon:
            path = data_directory / 'cache' / settings['cache']
            if not path.exists():
                cls._download(url=settings['url'], path=path)
            source_format = GradedLexiconFormat(settings['format'])
            if source_format is GradedLexiconFormat.PROFILE:
                sources.append(ProfileSource.load(name=settings['name'], path=path))
            else:
                order = [level.lower() for level in settings.get('levels', ())]
                sources.append(CefrlexSource.load(
                    name=settings['name'], path=path, order=order, threshold=threshold,
                ))
        return cls(sources=sources)

    @staticmethod
    def _download(*, url: str, path: pathlib.Path) -> None:
        request = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            payload = response.read()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)


COMMON_BANDS = ('top100', 'top1000')
# Below this gap the two disagree by a step, which is noise; at two steps it is a claim.
DISPUTE_GAP = 2


class LevelDispute:
    """Cards a count-derived lexicon places much earlier than their own level says.

    The lexicon cannot be taken at its word here — it records where a word turns up, so a rarity
    that appears once in a themed reader looks early. Frequency settles it: when a word is also
    among the commonest in the language, the early level is what a learner actually meets and the
    card's own level was guesswork; when it is rare, the early level is the artefact.
    """

    def __init__(
        self,
        *,
        lexicon: CefrlexSource,
        bands: typing.Sequence[str] = COMMON_BANDS,
        gap: int = DISPUTE_GAP,
    ):
        self._lexicon = lexicon
        self._bands = tuple(bands)
        self._gap = gap

    def contested(self, *, rows: typing.List[dict]) -> typing.List[typing.Tuple[dict, str]]:
        found = []
        for row in rows:
            ours = row.get('cefr', '').lower()
            theirs = self._lexicon.level_of(word=row['word'])
            if not ours or theirs is None or ours not in CEFR_LEVELS or theirs not in CEFR_LEVELS:
                continue
            if CEFR_LEVELS.index(ours) - CEFR_LEVELS.index(theirs) < self._gap:
                continue
            if row.get('frequency_band', '') in self._bands:
                found.append((row, theirs.upper()))
        return found


def first_part_of_speech(*, value: str) -> str:
    """A card's part-of-speech cell can hold several space-separated tokens; a profile is
    looked up on the first, or none when the cell is empty."""
    tokens = value.split()
    return tokens[0].lower() if tokens else ''


def _fill_words(
    *, lexicon: GradedLexicon, rows: typing.List[dict], built_keys: typing.AbstractSet[str],
) -> collections.Counter:
    """Give a word not yet built into a card the part of speech and level a profile states.

    A built word is skipped: its card already settled both, and changing the list row reopens it.
    """
    counts: collections.Counter = collections.Counter()
    for row in rows:
        if row['key'] in built_keys:
            continue
        # Part of speech first, since the level a profile gives depends on it.
        if not row.get('part_of_speech', '').strip():
            part_of_speech = lexicon.part_of_speech_of(word=row['word'])
            if part_of_speech is not None:
                row['part_of_speech'] = part_of_speech
                counts['part_of_speech'] += 1
        if not row.get('cefr', '').strip():
            graded = lexicon.level_of(
                word=row['word'], part_of_speech=first_part_of_speech(value=row.get('part_of_speech', '')),
            )
            if graded is not None:
                row['cefr'] = graded.level.upper()
                row['level_source'] = graded.source
                counts['cefr'] += 1
    return counts


def _relevel(
    *,
    lexicon: GradedLexicon,
    rows: typing.List[dict],
    bands: typing.Sequence[str],
    gap: int,
) -> typing.Dict[str, int]:
    """Apply every source in declared order, and count what each one changed.

    A profile source is consulted like any judgement stage: it states a level and the card takes
    it. A count-derived source instead only ever disputes a level already on the card, and only
    under the conditions `LevelDispute` enforces — so it runs after any profile has had its say.

    Where two profiles both answer a word they disagree, because each was compiled for a
    different stretch of the scale and each places a word it considers marginal at its own end.
    The first to answer wins, matching how a single word is looked up, so the order the language
    lists them in is the only place that choice is made. It is not a coin toss: a level that is
    too high leaves the card in a harder subdeck, while one too low marks the word as already
    learned and hides it, so the profile reaching higher is listed first.
    """
    counts: typing.Dict[str, int] = {}
    answered: typing.Set[str] = set()
    for source in lexicon.sources:
        if source.format is GradedLexiconFormat.PROFILE:
            changed = 0
            for row in rows:
                if row['key'] in answered:
                    continue
                level = source.level_of(
                    word=row['word'], part_of_speech=first_part_of_speech(value=row.get('part_of_speech', '')),
                )
                if level is None:
                    continue
                answered.add(row['key'])
                if level.upper() == row.get('cefr', ''):
                    continue
                row['cefr'] = level.upper()
                row['level_source'] = source.name
                changed += 1
        else:
            contested = LevelDispute(lexicon=source, bands=bands, gap=gap).contested(rows=rows)
            for row, level in contested:
                row['cefr'] = level
                row['level_source'] = source.name
            changed = len(contested)
        counts[source.name] = changed
    return counts


def main_for(
    *,
    language: language_config.LanguageConfig,
    root: pathlib.Path,
    levels: typing.Sequence[str],
    threshold: float = DEFAULT_DOCUMENT_THRESHOLD,
    dry_run: bool = False,
    relevel: bool = False,
    fill_words: bool = False,
    bands: typing.Sequence[str] = COMMON_BANDS,
    gap: int = DISPUTE_GAP,
) -> None:
    data_directory = language.data_directory(root=root)
    lexicon = GradedLexicon.load(language=language, data_directory=data_directory, threshold=threshold)
    if not lexicon.sources:
        print("no graded lexicon configured for this language — nothing to do")
        return

    if fill_words:
        words_path = data_directory / WORDS_FILENAME
        rows = language_config.TsvFile.read(words_path)
        cards_path = data_directory / CARDS_FILENAME
        built_keys = (
            {row['key'] for row in language_config.TsvFile.read(cards_path)} if cards_path.exists() else set()
        )
        counts = _fill_words(lexicon=lexicon, rows=rows, built_keys=built_keys)
        if not dry_run:
            language_config.TsvFile.write(words_path, rows=rows, columns=language.extracted_columns)
        print(f"filled part of speech on {counts['part_of_speech']} words, level on {counts['cefr']}")
        return

    if relevel:
        cards_path = data_directory / CARDS_FILENAME
        rows = language_config.TsvFile.read(cards_path)
        counts = _relevel(lexicon=lexicon, rows=rows, bands=bands, gap=gap)
        if not dry_run:
            language_config.TsvFile.write(cards_path, rows=rows, columns=language.card_columns)
        for name, changed in counts.items():
            print(f'{name}: {changed} cards changed')
        return

    words_path = data_directory / WORDS_FILENAME
    existing = language_config.TsvFile.read(words_path) if words_path.exists() else []
    known = {row['word'].strip().lower() for row in existing}
    wanted = lexicon.words_at(levels=levels)
    missing = {word: graded for word, graded in wanted.items() if word not in known}

    print(f'lexicon holds {len(lexicon)} words; {len(wanted)} at {", ".join(levels).upper()}')
    print(f'{len(missing)} of them are not in the word list')
    print('  by level:', dict(sorted(collections.Counter(graded.level for graded in missing.values()).items())))
    if dry_run:
        return

    importer = wordlist.WordlistImporter(language=language)
    imported = []
    for word, graded in sorted(missing.items()):
        row = importer.build_row(
            language=language, headword=word, native_translation='', pivot_translation='',
        )
        row['cefr'] = graded.level.upper()
        row['level_source'] = graded.source
        imported.append(row)
    rows, added = importer.merge_into(existing=existing, imported=imported)
    language_config.TsvFile.write(words_path, rows=rows, columns=language.extracted_columns)
    print(f'added {added} words to {words_path} — {len(rows)} in total')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--levels', default='a1,a2,b1',
        help="Comma-separated levels to import, e.g. a1,a2,b1",
    )
    parser.add_argument('--threshold', type=float, default=DEFAULT_DOCUMENT_THRESHOLD)
    parser.add_argument('--dry-run', action='store_true', help="Report what would change, change nothing")
    parser.add_argument(
        '--relevel', action='store_true',
        help="Re-level existing cards the lexicon and the frequency band both place earlier",
    )
    parser.add_argument(
        '--fill-words', action='store_true',
        help="Fill part of speech and level on word-list rows that have neither",
    )
    parser.add_argument(
        '--relevel-bands', default=','.join(COMMON_BANDS),
        help="Comma-separated frequency bands a card must be in before the lexicon may lower it",
    )
    parser.add_argument(
        '--relevel-gap', type=int, default=DISPUTE_GAP,
        help="How many levels the two must differ by before the disagreement counts",
    )
    language_config.add_language_arguments(parser)
    arguments = parser.parse_args()

    root = language_config.PROJECT_ROOT
    main_for(
        language=language_config.language_from(arguments, root=root),
        root=root,
        levels=[level.strip() for level in arguments.levels.split(',') if level.strip()],
        threshold=arguments.threshold,
        dry_run=arguments.dry_run,
        relevel=arguments.relevel,
        fill_words=arguments.fill_words,
        bands=[band.strip() for band in arguments.relevel_bands.split(',') if band.strip()],
        gap=arguments.relevel_gap,
    )


if __name__ == '__main__':
    main()
