"""Ask a reviewer whether a card teaches the sense of its word a learner would actually meet first.

A word imported from a graded lexicon arrives with nothing but its spelling — no translation, no
gloss, nothing to anchor which of its senses matters. The dictionary pass that followed had
nothing to anchor it either, so it simply took the first sense the target-language Wiktionary
listed. Wiktionary orders senses historically, not by how common they are, so its first sense is
often the oldest one on the page: `tarjeta` came back as an ornamental cartouche on a building
rather than the everyday card, `taco` as a wooden wedge rather than the food a beginner actually
asks for.

The full entry for a headword is already cached from that earlier pass, so this stage needs no
fresh lookup: it reads the same senses again and asks a reviewer to judge whether the one the card
teaches is the one a learner of the word meets first in ordinary speech, rewriting the card onto a
better sense when it is not.

Two steps, like the other model-backed stages: `--plan` writes the task files, `--from-json`
merges the answers.
"""

import argparse
import json
import pathlib
import typing

from . import dictionaries
from . import language_config
from . import senses
from . import tasks

CARDS_FILENAME = 'cards.tsv'
TASK_DIRECTORY = 'primary_sense'
INSTRUCTIONS_FILE = 'INSTRUCTIONS.md'
ANSWER_SUFFIX = '.json'
SLICE_COUNT = 8

# A single-sense entry had nothing to get wrong.
MINIMUM_CANDIDATES = 2

INSTRUCTIONS_TEMPLATE = """# Choosing the sense a learner meets first

Each row names a word in {target}, the sense its card currently teaches, and every sense the
dictionary lists for that headword.

Decide one thing: is the sense the card teaches the one a learner of this word meets first?

## Why this is being asked

The dictionary lists senses in historical order, not by how common they are. Its first sense is
often archaic, technical or regional, and the everyday meaning sits further down. A card built on
the first sense can end up teaching an ornament on a building instead of a card, or a wooden
wedge instead of the food.

## Output

Write `answer-NN.json` next to the task file, `NN` matching the task file's number:

```json
{{"rows": [
  {{"key": "<copied verbatim>", "verdict": "keep"}},
  {{"key": "<copied verbatim>", "verdict": "replace", "sense": 3,
   "definition": "<one plain sentence in {target}>",
   "translation": "<one to three words in {native}>"}}
]}}
```

One row per row in your task file, no omissions.

## Judging

`keep` when the current sense is the everyday one. Most rows are `keep` — the first sense is
often right, and replacing a sound card is worse than leaving it.

`replace` when a clearly commoner sense is in the list. Give its `number`, a `definition` and
a `translation`.

Ask which sense a beginner meets in ordinary speech, not which is oldest or most precise. A
sense marked for a trade, a sport, a science or one country is rarely the first one met.

`definition` is yours to write, not to copy: one plain sentence in {target} a beginner could read,
describing the thing itself. Do not reuse the dictionary's wording, which assumes the reader
already knows the word. No examples, no synonym lists.

`translation` is one to three words in {native}, comma-separated, for that sense only.

If no sense in the list is the everyday one — the word is a proper noun, regional slang, or the
dictionary simply lacks the common meaning — answer `keep` and add `"why": "<short reason>"`.

## Verifying

Before finishing, check with Python that the file parses, every `key` matches your task file
verbatim, every row is answered, and every `replace` row carries a non-empty `definition` and
`translation`. Report how many you kept and how many you replaced.
""" + tasks.INCREMENTAL_SAVING


class PrimarySensePlan:
    """Whether a card's headword teaches the sense a learner meets first, or should be rewritten."""

    def __init__(self, *, language: language_config.LanguageConfig, cache: dictionaries.Cache):
        self._language = language
        self._sense_plan = senses.SensePlan(language=language, cache=cache)

    def build(self, *, rows: typing.List[dict]) -> typing.List[dict]:
        built = []
        for row in rows:
            word = row.get('word', '')
            candidates = self._sense_plan.candidates_for(word=word)
            if len(candidates) < MINIMUM_CANDIDATES:
                continue
            built.append({
                'key': row['key'],
                'word': word,
                'current': {
                    'definition': row.get(self._language.definition_column, ''),
                    'meaning': row.get(self._language.translations_native_column, ''),
                },
                'senses': [
                    {'number': number, 'part_of_speech': candidate.part_of_speech, 'text': candidate.text}
                    for number, candidate in enumerate(candidates, start=1)
                ],
            })
        return built

    def merge(
        self, *, rows: typing.List[dict], answers: typing.Dict[str, dict],
    ) -> typing.Tuple[typing.List[dict], int, int]:
        replaced_count = 0
        kept_count = 0
        merged = []
        for row in rows:
            answer = answers.get(row['key'])
            if answer is None:
                merged.append(row)
                continue
            definition = str(answer.get('definition', '')).strip()
            translation = str(answer.get('translation', '')).strip()
            if answer.get('verdict') == 'replace' and definition and translation:
                updated = dict(row)
                updated[self._language.definition_column] = definition
                updated[self._language.translations_native_column] = translation
                # Everything downstream was written to show the sense being replaced: the
                # translations of the definition, and the example sentences that illustrate it.
                # Left alone they would argue with the card they sit on.
                stale = (
                    *self._language.definition_translation_columns,
                    *self._language.example_columns,
                    'examples_source',
                )
                for column in stale:
                    updated[column] = ''
                updated['definition_source'] = 'primary sense review'
                merged.append(updated)
                replaced_count += 1
            else:
                merged.append(row)
                kept_count += 1
        return merged, replaced_count, kept_count


class TaskFiles:
    """Task and answer files on disk, one slice per worker."""

    @staticmethod
    def write(
        *,
        tasks: typing.List[dict],
        directory: pathlib.Path,
        slices: int,
        language: language_config.LanguageConfig,
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
            INSTRUCTIONS_TEMPLATE.format(target=language.target_name, native=language.native_name),
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
    limit: typing.Optional[int] = None,
) -> None:
    data_directory = language.data_directory(root=root)
    cards_path = data_directory / CARDS_FILENAME
    rows = language_config.TsvFile.read(cards_path)
    if limit is not None:
        rows = rows[:limit]
    cache = dictionaries.Cache(
        root=data_directory / dictionaries.CACHE_DIRECTORY_NAME,
        refresh=False,
        edition=language.wiktionary_host,
    )
    primary_sense_plan = PrimarySensePlan(language=language, cache=cache)

    if plan:
        built = primary_sense_plan.build(rows=rows)
        if not built:
            print("no word has two or more candidate senses to choose the primary one from")
            return
        directory = data_directory / TASK_DIRECTORY
        written = TaskFiles.write(tasks=built, directory=directory, slices=slices, language=language)
        print(f"{len(built)} words have more than one candidate sense; wrote {len(written)} task files to {directory}")
        return

    if from_json is not None:
        answers = TaskFiles.load_answers(directory=from_json)
        rows, replaced_count, kept_count = primary_sense_plan.merge(rows=rows, answers=answers)
        language_config.TsvFile.write(path=cards_path, rows=rows, columns=language.card_columns)
        print(f"replaced {replaced_count} cards onto a commoner sense, kept {kept_count} as they were")
        return

    raise SystemExit("choose --plan or --from-json")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', action='store_true', help="Write task files for words with more than one candidate sense")
    parser.add_argument('--from-json', type=pathlib.Path, default=None, help="Directory holding answers")
    parser.add_argument('--slices', type=int, default=SLICE_COUNT)
    parser.add_argument('--limit', type=int, default=None, help="Process only the first N rows")
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
        limit=arguments.limit,
    )


if __name__ == '__main__':
    main()
