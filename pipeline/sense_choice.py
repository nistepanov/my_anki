"""Choose which dictionary sense a new word's card teaches, before anything is built from it.

A word that arrives with a translation already says which meaning it is; the model follows the
translation and the dictionary sense beside it only supplies wording. A word that arrives as a
bare spelling says nothing, and the dictionary pass used to take the first sense listed under
its part of speech. Wiktionary lists senses roughly in historical order, not by how common they
are, so that choice taught a stew as a cooking cauldron and a factor as a merchant's agent.

Nothing free ranks senses reliably: WordNet orders them by corpus frequency only where the corpus
saw the word, and for the rest its order is as arbitrary as any other. A model knows which sense
a learner needs, and naming a number is the cheapest question it can be asked. The answer is
recorded on the word list, so the dictionary pass builds the whole card — definition and related
words — from that one sense.

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

WORDS_FILENAME = 'words.tsv'
CARDS_FILENAME = 'cards.tsv'
TASK_DIRECTORY = 'sense_choice'
INSTRUCTIONS_FILE = 'INSTRUCTIONS.md'
TASK_PREFIX = 'task-'
ANSWER_PREFIX = 'answer-'
SLICE_COUNT = 2

# Enough to reach the everyday sense of a word whose entry opens with its history.
MAX_CANDIDATES = 6
MINIMUM_CANDIDATES = 2

INSTRUCTIONS_TEMPLATE = """# Choosing which meaning a card teaches

Each task file lists {target} words a learner who speaks {native}, at {levels} level, is about to
study,
with a numbered list of dictionary senses for the word's part of speech. The dictionary lists
senses in historical order, so the first one is often old or specialised.

For each word, pick the one sense a learner most needs: the meaning an ordinary reader meets
most often in present-day text. When two senses are close, prefer the more general one.

## Output

Write `answer-NN.json` next to `task-NN.json`, `NN` matching:

```json
{{"rows": [{{"key": "<copied verbatim>", "sense": 2}}]}}
```

One row per task row. `sense` is the number of the chosen sense, never outside the list.

## Verifying

Before finishing, check with Python that the file parses, every `key` belongs to your task file,
every task row is answered once, and every `sense` is within its row's list. Report those counts.
""" + tasks.INCREMENTAL_SAVING


class SenseChoice:
    """Which words need a sense chosen, and what each can choose from."""

    def __init__(self, *, language: language_config.LanguageConfig, cache: dictionaries.Cache):
        self._language = language
        self._cache = cache

    def candidates_for(self, *, row: dict) -> typing.List[str]:
        """The live senses stated for the word's part of speech, in the entry's own order."""
        lemma = dictionaries.RowEnricher.lookup_lemma(word=row['word'])
        cached = self._cache.load(source=dictionaries.Source.WIKTIONARY, lemma=lemma)
        if not cached or not cached.get('found'):
            return []
        parsed = dictionaries.WiktionaryClient._parse(wikitext=cached['wikitext'], language=self._language)
        # A word whose part of speech nobody has settled yet chooses among every live sense,
        # the way the dictionary pass does; asking for one of none would skip the word entirely.
        part_of_speech = row.get('part_of_speech', '').split()
        texts = [
            definition.text for definition in parsed.definitions
            if not definition.is_rare
            and (
                not part_of_speech
                or dictionaries.RowEnricher._category_matches(
                    category=definition.part_of_speech, pos_tokens=part_of_speech,
                )
            )
        ]
        return list(dict.fromkeys(texts))[:MAX_CANDIDATES]

    def needs_choice(self, *, row: dict, built_keys: typing.AbstractSet[str]) -> bool:
        # A translation already names the sense, and a built card has already settled it.
        return not (
            row['key'] in built_keys
            or row.get(self._language.translations_native_column, '').strip()
            or row.get(self._language.definition_column, '').strip()
        )

    def build(self, *, rows: typing.List[dict], built_keys: typing.AbstractSet[str]) -> typing.List[dict]:
        built = []
        for row in rows:
            if not self.needs_choice(row=row, built_keys=built_keys):
                continue
            candidates = self.candidates_for(row=row)
            if len(candidates) < MINIMUM_CANDIDATES:
                continue
            built.append({
                'key': row['key'],
                'word': row['word'],
                'part_of_speech': row.get('part_of_speech', ''),
                'senses': [{'number': number, 'text': text} for number, text in enumerate(candidates, start=1)],
            })
        return built

    def merge(
        self, *, rows: typing.List[dict], answers: typing.Dict[str, int], planned: typing.Dict[str, typing.List[str]],
    ) -> typing.Tuple[typing.List[dict], int]:
        chosen = 0
        for row in rows:
            senses = planned.get(row['key'])
            number = answers.get(row['key'])
            if senses is None or number is None or not 1 <= number <= len(senses):
                continue
            row[self._language.definition_column] = senses[number - 1]
            chosen += 1
        return rows, chosen


class TaskFiles:
    """Task and answer files on disk, one slice per worker."""

    @staticmethod
    def write(
        *, tasks_to_write: typing.List[dict], directory: pathlib.Path, slices: int,
        language: language_config.LanguageConfig,
    ) -> typing.List[pathlib.Path]:
        directory.mkdir(parents=True, exist_ok=True)
        size = -(-len(tasks_to_write) // slices) if tasks_to_write else 0
        written = []
        for index in range(slices):
            chunk = tasks_to_write[index * size:(index + 1) * size] if size else []
            if not chunk:
                continue
            path = directory / f'{TASK_PREFIX}{index + 1:02d}.json'
            path.write_text(json.dumps({'rows': chunk}, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
            written.append(path)
        (directory / INSTRUCTIONS_FILE).write_text(
            INSTRUCTIONS_TEMPLATE.format(
                target=language.target_name,
                native=language.native_name,
                levels=language.studied_level_range,
            ),
            encoding='utf-8',
        )
        return written

    @staticmethod
    def load_planned(*, directory: pathlib.Path) -> typing.Dict[str, typing.List[str]]:
        """The sense texts each number stood for, read from the task files that numbered them."""
        planned = {}
        for path in sorted(directory.glob(f'{TASK_PREFIX}*.json')):
            for row in json.loads(path.read_text(encoding='utf-8'))['rows']:
                planned[row['key']] = [sense['text'] for sense in row['senses']]
        return planned

    @staticmethod
    def load_answers(*, directory: pathlib.Path) -> typing.Dict[str, int]:
        answers = {}
        for path in sorted(directory.glob(f'{ANSWER_PREFIX}*.json')):
            for row in json.loads(path.read_text(encoding='utf-8')).get('rows', []):
                answers[row['key']] = int(row['sense'])
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
    words_path = data_directory / WORDS_FILENAME
    cards_path = data_directory / CARDS_FILENAME
    rows = language_config.TsvFile.read(words_path)
    built_keys = {row['key'] for row in language_config.TsvFile.read(cards_path)} if cards_path.exists() else set()
    cache = dictionaries.Cache(
        root=data_directory / dictionaries.CACHE_DIRECTORY_NAME,
        refresh=False,
        edition=language.wiktionary_host,
    )
    choice = SenseChoice(language=language, cache=cache)
    directory = data_directory / TASK_DIRECTORY

    if plan:
        built = choice.build(rows=rows, built_keys=built_keys)
        if not built:
            print("no new word has more than one sense to choose from")
            return
        written = TaskFiles.write(tasks_to_write=built, directory=directory, slices=slices, language=language)
        print(f"{len(built)} words need a sense chosen; wrote {len(written)} task files to {directory}")
        return

    if from_json is not None:
        rows, chosen = choice.merge(
            rows=rows,
            answers=TaskFiles.load_answers(directory=from_json),
            planned=TaskFiles.load_planned(directory=directory),
        )
        language_config.TsvFile.write(words_path, rows=rows, columns=language.extracted_columns)
        print(f"recorded a chosen sense on {chosen} words")
        return

    raise SystemExit("choose --plan or --from-json")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', action='store_true', help="Write task files for words with several senses")
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
