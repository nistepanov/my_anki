"""Give a card the other meanings of its headword, beyond the one sense it was built to teach.

A source word list carries one entry per sense it happened to include, so a polysemous word like
`draft` ends up with a card for the document and another for the verb, while the dictionary knows
several more — a current of air, a military callup, beer from the tap. Those senses are real and a
learner will meet them, but nothing upstream ever asked the dictionary for the rest of an entry, so
they are simply missing.

The full Wiktionary page for every headword is already cached from the earlier dictionary pass, so
this needs no network access: it re-reads the same wikitext and keeps whatever senses the first
pass had no use for.

Two steps, like the other model-backed stages: `--plan` writes the task files, `--from-json`
merges the answers.
"""

import argparse
import json
import pathlib
import typing

from . import dictionaries
from . import language_config
from . import tasks

CARDS_FILENAME = 'cards.tsv'
TASK_DIRECTORY = 'senses'
INSTRUCTIONS_FILE = 'INSTRUCTIONS.md'
ANSWER_SUFFIX = '.json'
SLICE_COUNT = 8

# A row with fewer candidates than this has nothing to show beyond the sense it already teaches.
MINIMUM_CANDIDATES = 3
MAX_SENSES_PER_WORD = 4
# A pipe, not a comma: the synonym columns already use a comma, and a comma inside one element
# silently shifts every later pairing in the row.
SENSE_SEPARATOR = ' | '

INSTRUCTIONS_TEMPLATE = """# Choosing a headword's other meanings

Each task file lists words in {target} with the sense their card already teaches and a numbered
list of candidate senses pulled from the dictionary. For each word, choose the other meanings a
learner at this level would actually meet.

## Output

Write `answer-NN.json` next to the task file, `NN` matching the task file's number:

```json
{{"rows": [{{"key": "<copied verbatim>",
   "senses": [{{"native": "<the meaning in {native}>",
               "example": "<a short sentence in {target} using that meaning>"}}]}}]}}
```

One row per word you answer; a row you skip entirely is left unanswered, so answer every row in
your task file. `senses` is a plain list — nothing addresses a candidate by its number, so there is
nothing to keep consistent between the task file and the answer beyond the list itself.

## What to choose

Pick from the numbered candidates only the meanings an ordinary, current use of the word would
show a learner. Skip anything obsolete, dialectal, technical jargon, or taxonomic, and skip a
candidate that only rephrases a meaning you already chose.

At most four senses per word, fewer when the word genuinely has fewer. An empty list is a correct
answer for a word whose other senses are all marginal — do not pad it with a weak candidate just
to have something to show.

Never include the sense given as the row's own — it is already on the card and repeating it here
would teach nothing new.

## Writing a sense

`native` is the meaning written in {native}, and it is short: a word or a handful of words, not a
sentence, and no explanation in brackets beyond a register mark.

`example` is a very short sentence, well under ten words, plain and present-day, using the
headword in exactly that sense.

Never put a vertical bar `|` in either field, and never a newline. The bar is the separator the
card splits the list on, and one inside a field would cut it in two.

## Verifying

Before finishing, check with Python that the file parses, every `key` belongs to your task file,
no row's `senses` exceeds four entries, and no field contains a bar. Report those counts.
""" + tasks.INCREMENTAL_SAVING


class SensePlan:
    """The candidate senses a card's headword has beyond the one it teaches."""

    def __init__(self, *, language: language_config.LanguageConfig, cache: dictionaries.Cache):
        self._language = language
        self._cache = cache
        self._candidates_by_word: typing.Dict[str, typing.List[dictionaries.WiktionaryDefinition]] = {}

    def candidates_for(self, *, word: str) -> typing.List[dictionaries.WiktionaryDefinition]:
        """A word's cleaned senses, read once per word since many rows share a headword."""
        if word not in self._candidates_by_word:
            cached = self._cache.load(source=dictionaries.Source.WIKTIONARY, lemma=word)
            wikitext = cached.get('wikitext', '') if cached and cached.get('found') else ''
            self._candidates_by_word[word] = (
                dictionaries.WiktionaryClient.senses(wikitext=wikitext, language=self._language)
                if wikitext else []
            )
        return self._candidates_by_word[word]

    def build(self, *, rows: typing.List[dict]) -> typing.List[dict]:
        built = []
        for row in rows:
            word = row.get('word', '')
            candidates = self.candidates_for(word=word)
            if len(candidates) < MINIMUM_CANDIDATES:
                continue
            built.append({
                'key': row['key'],
                'word': word,
                'own': {
                    'definition': row.get(self._language.definition_column, ''),
                    'meaning': row.get(self._language.translations_native_column, ''),
                },
                'candidates': [
                    {'number': number, 'part_of_speech': candidate.part_of_speech, 'text': candidate.text}
                    for number, candidate in enumerate(candidates, start=1)
                ],
            })
        return built

    def merge(
        self, *, rows: typing.List[dict], answers: typing.Dict[str, dict],
    ) -> typing.Tuple[typing.List[dict], int, int]:
        target_column, native_column = self._language.other_senses_columns
        filled_senses = 0
        answered_words = 0
        merged = []
        for row in rows:
            answer = answers.get(row['key'])
            if answer is None:
                merged.append(row)
                continue
            senses = [
                (
                    self._single_field(text=str(sense.get('native', ''))),
                    self._single_field(text=str(sense.get('example', ''))),
                )
                for sense in answer.get('senses', [])[:MAX_SENSES_PER_WORD]
            ]
            senses = [(meaning, example) for meaning, example in senses if meaning and example]
            updated = dict(row)
            updated[native_column] = SENSE_SEPARATOR.join(meaning for meaning, _ in senses)
            updated[target_column] = SENSE_SEPARATOR.join(example for _, example in senses)
            filled_senses += len(senses)
            answered_words += 1
            merged.append(updated)
        return merged, filled_senses, answered_words

    @staticmethod
    def _single_field(*, text: str) -> str:
        """A sense with its separator defused, so a stray bar cannot split it in two.

        The instructions ask the model never to write one, but a bar copied in from a dictionary
        entry would silently shift every sense after it, so it is rewritten rather than trusted.
        """
        return ' '.join(text.split()).replace('|', ';')


class TaskFiles:
    """Task and answer files on disk, one slice per worker."""

    @staticmethod
    def write(
        *, tasks: typing.List[dict], directory: pathlib.Path, slices: int, language: language_config.LanguageConfig,
    ) -> typing.List[pathlib.Path]:
        directory.mkdir(parents=True, exist_ok=True)
        size = -(-len(tasks) // slices) if tasks else 0
        written = []
        for index in range(slices):
            chunk = tasks[index * size:(index + 1) * size] if size else []
            if not chunk:
                continue
            path = directory / f'task-{index + 1:02d}{ANSWER_SUFFIX}'
            path.write_text(json.dumps({'rows': chunk}, ensure_ascii=False, indent=1), encoding='utf-8')
            written.append(path)
        (directory / INSTRUCTIONS_FILE).write_text(
            INSTRUCTIONS_TEMPLATE.format(
                target=language.target_name, native=language.native_name,
            ),
            encoding='utf-8',
        )
        return written

    @staticmethod
    def load_answers(*, directory: pathlib.Path) -> typing.Dict[str, dict]:
        answers: typing.Dict[str, dict] = {}
        if not directory.exists():
            return answers
        for path in sorted(directory.glob(f'answer-*{ANSWER_SUFFIX}')):
            for row in json.loads(path.read_text(encoding='utf-8')).get('rows', []):
                answers[row['key']] = row
        return answers


def main_for(
    *,
    language: language_config.LanguageConfig,
    root: pathlib.Path,
    plan: bool = False,
    from_json: typing.Optional[pathlib.Path] = None,
    slices: int = SLICE_COUNT,
) -> None:
    data_directory = language.data_directory(root=root)
    cards_path = data_directory / CARDS_FILENAME
    rows = language_config.TsvFile.read(cards_path)
    cache = dictionaries.Cache(
        root=data_directory / dictionaries.CACHE_DIRECTORY_NAME,
        refresh=False,
        edition=language.wiktionary_host,
    )
    sense_plan = SensePlan(language=language, cache=cache)

    if plan:
        built = sense_plan.build(rows=rows)
        if not built:
            print("no word has three or more candidate senses beyond the one it already teaches")
            return
        directory = data_directory / TASK_DIRECTORY
        written = TaskFiles.write(tasks=built, directory=directory, slices=slices, language=language)
        print(f"{len(built)} words have unlearned senses; wrote {len(written)} task files to {directory}")
        return

    if from_json is not None:
        answers = TaskFiles.load_answers(directory=from_json)
        rows, filled_senses, answered_words = sense_plan.merge(rows=rows, answers=answers)
        language_config.TsvFile.write(path=cards_path, rows=rows, columns=language.card_columns)
        print(f"filled {filled_senses} other senses across {answered_words} answered words")
        return

    raise SystemExit("choose --plan or --from-json")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', action='store_true', help="Write task files for words with unlearned senses")
    parser.add_argument('--from-json', type=pathlib.Path, default=None, help="Directory holding answers")
    parser.add_argument('--slices', type=int, default=SLICE_COUNT)
    language_config.add_language_arguments(parser)
    arguments = parser.parse_args()

    if not arguments.plan and arguments.from_json is None:
        parser.error("choose --plan or --from-json")

    root = language_config.PROJECT_ROOT
    main_for(
        language=language_config.language_from(arguments, root=root),
        root=root,
        plan=arguments.plan,
        from_json=arguments.from_json,
        slices=arguments.slices,
    )


if __name__ == '__main__':
    main()
