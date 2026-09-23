"""Teach a book's vocabulary: the words in it a learner does not know yet, each on a phrase from the book.

The reading a learner is actually doing is the best source of words at this level, and a phrase
the author wrote beats an invented example — the learner meets the word again where it came from.

Two steps, because the middle one is a model's: `tasks` writes the words with their phrases into
batches, a model answers each batch with a translation, a definition and a verdict, and `rows`
turns the kept answers into deck rows.
"""

import argparse
import collections
import csv
import html
import json
import pathlib
import re
import typing
import zipfile

import wordfreq

from . import anki
from . import frequency
from . import language_config

# How much of the frequency list the learner is taken to know already. Words inside it are
# skipped even when the book makes them look rare, since the guess is usually an inflected form.
KNOWN_WORD_COUNT = 6000
# Rarer than this and the word belongs to the book's own century rather than to the language.
MINIMUM_ZIPF = 3.0
# A phrase shorter than this teaches nothing; longer than that and it stops being a phrase.
MINIMUM_PHRASE_WORDS = 5
MAXIMUM_PHRASE_WORDS = 20
COMFORTABLE_PHRASE_WORDS = 12
# One batch is what a model answers in a single sitting without losing the thread.
BATCH_SIZE = 110
# Levels the learner has finished, whatever a published list says about a word's rarity.
KNOWN_LEVELS = {'A1', 'A2', 'B1'}
LEVEL_LISTS = (('cefrj', 'cefrj.csv'), ('octanove', 'octanove.csv'))
# Old books carry racial language that must not reach a card, whatever word it illustrates.
OFFENSIVE_TERMS = ('negro', 'savages', 'coolie', 'redskin')

WORD_PATTERN = re.compile(r"[A-Za-z][A-Za-z'-]*")
LOWERCASE_WORD_PATTERN = re.compile(r"[A-Za-z][a-z]+(?:-[a-z]+)*")
SENTENCE_BREAK_PATTERN = re.compile(r'(?<=[.!?])\s+(?=[‘“"A-Z])')
CLAUSE_BREAK_PATTERN = re.compile(r'(?<=[,;:—–])\s+')
TAG_PATTERN = re.compile(r'<[^>]+>')
SCRIPT_PATTERN = re.compile(r'<(script|style)[^>]*>.*?</\1>', re.S)
PARAGRAPH_PATTERN = re.compile(r'<p[^>]*>(.*?)</p>', re.S)
EDGE_CHARACTERS = '‘’“”"\' '
# Endings stripped to guess a headword; the frequency table then says which guess is a real word.
INFLECTIONS = (('ies', 'y'), ('es', ''), ('s', ''), ('ied', 'y'), ('ed', ''), ('ed', 'e'), ('ing', ''), ('ing', 'e'), ("'s", ''))
SHORTEST_STEM = 3


class Book:
    """An epub read as a flat list of sentences, in reading order."""

    def __init__(self, *, sentences: typing.List[str]):
        self.sentences = sentences

    @classmethod
    def read(cls, *, path: pathlib.Path) -> 'Book':
        archive = zipfile.ZipFile(path)
        documents = sorted(name for name in archive.namelist() if name.endswith(('.html', '.xhtml', '.htm')))
        sentences = []
        for name in documents:
            markup = archive.read(name).decode('utf-8', 'ignore')
            for paragraph in PARAGRAPH_PATTERN.findall(SCRIPT_PATTERN.sub(' ', markup)):
                sentences.extend(cls._sentences(markup=paragraph))
        return cls(sentences=sentences)

    @staticmethod
    def _sentences(*, markup: str) -> typing.List[str]:
        text = re.sub(r'\s+', ' ', html.unescape(TAG_PATTERN.sub(' ', markup))).strip()
        return [sentence.strip() for sentence in SENTENCE_BREAK_PATTERN.split(text) if sentence.strip()]

    def occurrences(self) -> typing.Dict[str, typing.List[typing.Tuple[int, str]]]:
        """Where each headword appears, by the form it wears there.

        Only lowercase forms count: a capitalised word is usually a name, and a name is not
        vocabulary. A word that the book only ever capitalises therefore never shows up here.
        """
        found: typing.Dict[str, typing.List[typing.Tuple[int, str]]] = collections.defaultdict(list)
        for index, sentence in enumerate(self.sentences):
            for match in LOWERCASE_WORD_PATTERN.finditer(sentence):
                form = match.group(0)
                if not form[0].isupper():
                    found[self.headword(form=form)].append((index, form))
        return found

    @staticmethod
    def headword(*, form: str) -> str:
        """The likeliest dictionary form of an inflected word, judged by which guess is commoner."""
        guesses = {form}
        for suffix, replacement in INFLECTIONS:
            if form.endswith(suffix) and len(form) - len(suffix) >= SHORTEST_STEM:
                stem = form[: -len(suffix)] + replacement
                guesses.add(stem)
                if not replacement and len(stem) > SHORTEST_STEM and stem[-1] == stem[-2]:
                    guesses.add(stem[:-1])
        return max(guesses, key=lambda guess: (wordfreq.zipf_frequency(guess, 'en'), guess == form))


class Phrase:
    """The run of clauses around a word, cut to something a learner can read in one breath."""

    @staticmethod
    def around(*, sentence: str, form: str) -> typing.Optional[str]:
        clauses = [clause for clause in CLAUSE_BREAK_PATTERN.split(sentence) if clause.strip()]
        held = next((index for index, clause in enumerate(clauses) if re.search(rf'\b{re.escape(form)}\b', clause)), None)
        if held is None:
            return None
        first = last = held
        while Phrase._length(clauses=clauses[first:last + 1]) < MINIMUM_PHRASE_WORDS + 3:
            room = MAXIMUM_PHRASE_WORDS - Phrase._length(clauses=clauses[first:last + 1])
            if last < len(clauses) - 1 and Phrase._length(clauses=[clauses[last + 1]]) <= room:
                last += 1
            elif first > 0 and Phrase._length(clauses=[clauses[first - 1]]) <= room:
                first -= 1
            else:
                break
        phrase = ' '.join(clauses[first:last + 1]).strip(EDGE_CHARACTERS).rstrip(',;:—–').strip(EDGE_CHARACTERS)
        length = len(WORD_PATTERN.findall(phrase))
        return phrase if MINIMUM_PHRASE_WORDS <= length <= MAXIMUM_PHRASE_WORDS + 5 else None

    @staticmethod
    def _length(*, clauses: typing.Sequence[str]) -> int:
        return len(WORD_PATTERN.findall(' '.join(clauses)))

    @staticmethod
    def readability(*, phrase: str, form: str, known: typing.Set[str]) -> float:
        """How much of the phrase besides the word itself the learner already reads."""
        others = [word.lower() for word in WORD_PATTERN.findall(phrase) if word.lower() != form.lower()]
        if not others:
            return -1
        share = sum(word in known for word in others) / len(others)
        return share - abs(len(others) - COMFORTABLE_PHRASE_WORDS) / 100


class Vocabulary:
    """What the learner is taken to know: the common words, plus every word the deck already has."""

    def __init__(self, *, known: typing.Set[str], in_deck: typing.Set[str]):
        self.known = known
        self.in_deck = in_deck

    @classmethod
    def load(cls, *, rows: typing.Sequence[dict]) -> 'Vocabulary':
        in_deck = set()
        for row in rows:
            word = row['word'].strip().lower()
            in_deck.add(word[3:] if word.startswith('to ') else word)
        return cls(known=set(wordfreq.top_n_list('en', KNOWN_WORD_COUNT)), in_deck=in_deck)

    def teaches(self, *, headword: str, forms: typing.Iterable[str]) -> bool:
        if headword in self.known or headword in self.in_deck or '-' in headword:
            return False
        return not any(form in self.known for form in forms)


def write_tasks(*, book: Book, vocabulary: Vocabulary, directory: pathlib.Path) -> int:
    """Write one task file per batch: each word with the phrase a model should build its card on."""
    occurrences = book.occurrences()
    tasks = []
    for headword, places in occurrences.items():
        if not vocabulary.teaches(headword=headword, forms=(form for _, form in places)):
            continue
        if wordfreq.zipf_frequency(headword, 'en') < MINIMUM_ZIPF:
            continue
        choices = []
        for index, form in places:
            phrase = Phrase.around(sentence=book.sentences[index], form=form)
            if phrase is not None:
                choices.append((Phrase.readability(phrase=phrase, form=form, known=vocabulary.known), index, form, phrase))
        if not choices:
            continue
        _, _, form, phrase = max(choices)
        tasks.append({
            'form': form,
            'lemma_guess': headword,
            'phrase': phrase,
            'book_count': len(places),
            'first_seen': min(index for index, _ in places),
        })

    tasks.sort(key=lambda task: task['first_seen'])
    for number, task in enumerate(tasks):
        task['id'] = number
    directory.mkdir(parents=True, exist_ok=True)
    (directory / 'tasks_all.json').write_text(json.dumps(tasks, ensure_ascii=False), encoding='utf-8')
    for start in range(0, len(tasks), BATCH_SIZE):
        batch = directory / f'task_{start // BATCH_SIZE:02d}.json'
        batch.write_text(json.dumps(tasks[start:start + BATCH_SIZE], ensure_ascii=False, indent=1), encoding='utf-8')
    return len(tasks)


class PublishedLevels:
    """The CEFR level a graded word list gives a word, where one of them lists it at all."""

    def __init__(self, *, by_word: typing.Dict[str, typing.Tuple[str, str]]):
        self._by_word = by_word

    @classmethod
    def load(cls, *, cache: pathlib.Path) -> 'PublishedLevels':
        by_word: typing.Dict[str, typing.Tuple[str, str]] = {}
        for source, name in LEVEL_LISTS:
            with (cache / name).open(encoding='utf-8') as stream:
                for record in csv.DictReader(stream):
                    level = record['CEFR'].strip().upper()
                    for spelling in record['headword'].split('/'):
                        word = spelling.strip().lower()
                        if word and (word not in by_word or level < by_word[word][0]):
                            by_word[word] = (level, source)
        return cls(by_word=by_word)

    def of(self, *, word: str) -> typing.Tuple[str, str]:
        return self._by_word.get(word, ('', ''))


def build_rows(
    *,
    directory: pathlib.Path,
    subdeck: str,
    language: language_config.LanguageConfig,
    root: pathlib.Path,
    columns: typing.Sequence[str],
    vocabulary: Vocabulary,
) -> typing.Tuple[typing.List[dict], collections.Counter]:
    """Turn every answered batch into deck rows, dropping what the model or the lists rule out."""
    tasks = {task['id']: task for task in json.loads((directory / 'tasks_all.json').read_text(encoding='utf-8'))}
    answers = [
        answer
        for path in sorted(directory.glob('answer_*.json'))
        for answer in json.loads(path.read_text(encoding='utf-8'))
    ]
    levels = PublishedLevels.load(cache=language.data_directory(root=root) / 'cache')
    bands = frequency.FrequencyBands(language_code=language.target)

    skipped: collections.Counter = collections.Counter()
    rows, taken = [], set()
    for answer in sorted(answers, key=lambda item: item['id']):
        task = tasks[answer['id']]
        lemma = answer.get('lemma', '').strip().lower()
        if not answer.get('keep'):
            skipped['the model dropped it'] += 1
        elif task['form'] not in answer['phrase_en']:
            skipped['the phrase lost the word'] += 1
        elif any(term in answer['phrase_en'].lower() for term in OFFENSIVE_TERMS):
            skipped['the phrase carries a racial term'] += 1
        elif lemma in vocabulary.known or lemma in vocabulary.in_deck:
            skipped['known already or in the deck'] += 1
        elif levels.of(word=lemma)[0] in KNOWN_LEVELS:
            skipped['a list puts it below B2'] += 1
        elif lemma in taken:
            skipped['a second form of a word already taken'] += 1
        else:
            taken.add(lemma)
            rows.append(_row(answer=answer, task=task, subdeck=subdeck, columns=columns, levels=levels, bands=bands))
    return rows, skipped


def _row(
    *,
    answer: dict,
    task: dict,
    subdeck: str,
    columns: typing.Sequence[str],
    levels: PublishedLevels,
    bands: frequency.FrequencyBands,
) -> dict:
    lemma = answer['lemma'].strip().lower()
    level, level_source = levels.of(word=lemma)
    band = bands.band_for(word=lemma)
    is_verb = answer['pos'] == 'verb'
    tags = [answer['pos'], f'freq::{band}', f'source::{subdeck.lower().replace(" ", "_")}']
    row = {column: '' for column in columns}
    row.update({
        'key': f"{'to ' if is_verb else ''}{lemma} ({answer['translation_ru'].split(',')[0].strip()})",
        'word': lemma,
        'article': 'to' if is_verb else '',
        'part_of_speech': answer['pos'],
        'translations_ru': answer['translation_ru'],
        'definition_en': answer['definition_en'],
        'definition_ru': answer['definition_ru'],
        'examples_en': answer['phrase_en'],
        'examples_ru': answer['phrase_ru'],
        'examples_source': 'book',
        'definition_source': 'llm',
        'context_index': '1',
        'context_target': task['form'],
        'cefr': level,
        'level_source': level_source,
        'frequency_band': band,
        'status': 'new',
        'subdeck': subdeck,
        'tags': ' '.join(tags + ([f'cefr::{level}'] if level else [])),
    })
    # A tab or a newline in a cell would break the table.
    return {column: ' '.join(value.split()) for column, value in row.items()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage', choices=('tasks', 'rows'), help="Write the model's task files, or read its answers")
    parser.add_argument('--directory', required=True, type=pathlib.Path, help="Where the task and answer files live")
    parser.add_argument('--epub', type=pathlib.Path, help="The book, for the task stage")
    parser.add_argument('--subdeck', help="Subdeck the book's words land in, for the row stage")
    language_config.add_language_arguments(parser)
    arguments = parser.parse_args()

    root = language_config.PROJECT_ROOT
    language = language_config.language_from(arguments, root=root)
    table_path = language.data_directory(root=root) / 'cards.tsv'
    columns = table_path.read_text(encoding='utf-8').split('\n', 1)[0].split('\t')
    if 'subdeck' not in columns:
        columns.append('subdeck')
    table = language_config.TsvFile.read(table_path)
    vocabulary = Vocabulary.load(rows=table)

    if arguments.stage == 'tasks':
        if arguments.epub is None:
            raise SystemExit("the task stage needs --epub")
        count = write_tasks(book=Book.read(path=arguments.epub), vocabulary=vocabulary, directory=arguments.directory)
        print(f"{count} words to ask about, in batches of {BATCH_SIZE}, under {arguments.directory}")
        return

    if not arguments.subdeck:
        raise SystemExit("the row stage needs --subdeck")
    rows, skipped = build_rows(
        directory=arguments.directory,
        subdeck=arguments.subdeck,
        language=language,
        root=root,
        columns=columns,
        vocabulary=vocabulary,
    )
    for row in rows:
        assert anki.NotePusher.render_context(row=row, language=language), row['key']
    language_config.TsvFile.write(table_path, rows=table + rows, columns=columns)
    language_config.TsvFile.write(arguments.directory / 'rows.tsv', rows=rows, columns=columns)
    print(f"{len(rows)} new rows | skipped {dict(skipped)}")


if __name__ == '__main__':
    main()
