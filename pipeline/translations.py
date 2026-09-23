"""Give every example sentence a translation into each of the learner's languages.

Sources hand over sentences translated into one language only — a corpus has whatever its
contributors wrote, and the model was asked for the learner's own language. The card shows
both, so the halves have to be filled in.

Run in two steps, the same shape as the other model-backed stage: `--plan` reorders the
examples best-first and writes the gaps out as task files, then `--from-json` merges the
answers back. Reordering first is what makes the task files safe to answer — a sentence is
addressed by its line number, so the lines must stop moving before anyone refers to them.
"""

import argparse
import json
import pathlib
import sys
import typing

from . import examples
from . import language_config
from . import tasks

CARDS_FILE = 'cards.tsv'
TASK_DIRECTORY = 'examples'
INSTRUCTIONS_FILE = 'INSTRUCTIONS.md'
ANSWER_SUFFIX = '.json'
SLICE_COUNT = 8

# Rendered next to the task files so a worker has the rules and the data in one place.
INSTRUCTIONS_TEMPLATE = """# Translating example sentences

Each task file holds words with their {target} example sentences. A sentence carries the
translations it already has under `have`, and the languages it still needs under `missing`.
Translate only what is listed as missing.

Languages by code: {languages}.

## Output

Write one answer file next to the task file, named `answer-NN.json` where `NN` matches the
task file's number. It must parse with `json.load` — UTF-8, no markdown fence, no commentary:

```json
{{"rows": [
  {{"key": "<copied verbatim from the task>",
   "definition": {{"{example_suffix}": "<translation of the definition>"}},
   "sentences": [{{"line": 0, "{example_suffix}": "<translation of that sentence>"}}]}}
]}}
```

`line` addresses the sentence within its word and must be copied verbatim — never renumbered.
Give one entry per missing language on that sentence; a sentence needing two languages gets
both codes in the same object. Skip nothing: a missing `line` silently leaves that card
half-translated.

A row carrying a `definition` needs that translated too, into the languages its own `missing`
lists. Omit the `definition` key entirely for rows that do not have one.

## Rules

The definition is a dictionary definition and reads as one: a noun phrase describing the thing,
not a sentence about it, and not a translation of the headword. `Parte del cuerpo donde están el
cerebro` becomes `Часть тела, где находятся мозг…`, never `голова`. Keep it at the same plain
level as the original — it exists so a beginner can read it.

Translate the sentence, not the word. The translation reads as something a person would say,
matches the register of the original, and keeps its tense and number. Where the sentence
already has a translation into the other language, stay consistent with how it read there.

The word's own meaning is given as `meaning`; when the sentence is ambiguous, that is the
sense it belongs to. Do not add explanations, alternatives in brackets, or transliteration.


## Verifying

Before finishing, check with Python that the file parses, every `key` appears in your task
file, every `line` appears under that key, and that you produced exactly as many translations
as the task file asked for. Report those counts.
""" + tasks.INCREMENTAL_SAVING


class TranslationPlan:
    """The sentences a card will show that are still missing a translation."""

    def __init__(self, *, language: language_config.LanguageConfig):
        self._language = language
        self._suffixes = examples.ExampleSet.translation_suffixes(language=language)

    def reorder(self, *, rows: typing.List[dict]) -> typing.List[dict]:
        """Put each row's best sentences first, so the card's choice is visible in the file."""
        reordered = []
        for row in rows:
            ranked = examples.ExampleSet.rank(
                examples=examples.ExampleSet.read(row=row, language=self._language),
                suffixes=self._suffixes,
            )
            reordered.append(examples.ExampleSet.write(row=row, examples=ranked, language=self._language))
        return reordered

    def definition_gap(self, *, row: dict) -> typing.Optional[dict]:
        """The definition needs the same treatment as a sentence, and for the same reason."""
        target = row.get(self._language.definition_column, '').strip()
        if not target:
            return None
        missing = [
            suffix for suffix, column in zip(self._suffixes, self._language.definition_translation_columns)
            if not row.get(column, '').strip()
        ]
        return {'target': target, 'missing': missing} if missing else None

    def build(self, *, rows: typing.List[dict]) -> typing.List[dict]:
        tasks = []
        for row in rows:
            shown = examples.ExampleSet.read(row=row, language=self._language)[:examples.CARD_EXAMPLE_LIMIT]
            sentences = [
                {
                    'line': index,
                    'target': example.target,
                    'missing': example.missing(suffixes=self._suffixes),
                    'have': {
                        suffix: text
                        for suffix, text in example.translations.items()
                        if text
                    },
                }
                for index, example in enumerate(shown)
                if example.missing(suffixes=self._suffixes)
            ]
            definition = self.definition_gap(row=row)
            if sentences or definition is not None:
                task = {
                    'key': row['key'],
                    'word': row['word'],
                    'meaning': row[self._language.translations_native_column],
                    'sentences': sentences,
                }
                if definition is not None:
                    task['definition'] = definition
                tasks.append(task)
        return tasks

    def merge(
        self,
        *,
        rows: typing.List[dict],
        answers: typing.Dict[str, dict],
        planned: typing.Dict[str, typing.Dict[int, str]],
    ) -> typing.Tuple[typing.List[dict], int, int]:
        """Merge answers back, resolving each line number through the sentence it named.

        Taking the line number as a position is what breaks the cards: the number is only valid
        against the order the task file was written from, and nothing holds that order still
        between the two halves of this stage. A translation that lands one sentence over is
        invisible — the merge writes only into empty slots, so it never collides with anything —
        and the card looks finished while teaching the wrong pairing.
        """
        filled = 0
        unresolved = 0
        merged = []
        for row in rows:
            answer = answers.get(row['key'])
            if answer is None:
                merged.append(row)
                continue
            answered_definition = answer.get('definition') or {}
            for suffix, column in zip(self._suffixes, self._language.definition_translation_columns):
                text_value = str(answered_definition.get(suffix, '')).strip()
                if text_value and not row.get(column, '').strip():
                    row[column] = text_value
                    filled += 1
            current = examples.ExampleSet.read(row=row, language=self._language)
            for sentence in answer.get('sentences', []):
                index = self._resolve(sentence=sentence, planned=planned.get(row['key'], {}), current=current)
                if index is None:
                    unresolved += 1
                    continue
                example = current[index]
                for suffix in self._suffixes:
                    text = str(sentence.get(suffix, '')).strip()
                    if text and not example.translations.get(suffix):
                        example.translations[suffix] = text
                        filled += 1
            merged.append(examples.ExampleSet.write(row=row, examples=current, language=self._language))
        return merged, filled, unresolved

    @staticmethod
    def _resolve(
        *,
        sentence: dict,
        planned: typing.Dict[int, str],
        current: typing.Sequence[examples.Example],
    ) -> typing.Optional[int]:
        """The line number an answer carries, turned back into the sentence the task named."""
        line = sentence.get('line')
        if not isinstance(line, int):
            return None
        target = planned.get(line)
        if target is None:
            return None
        return examples.ExampleSet.locate(examples=current, target=target)


class TaskFiles:
    """Task and answer files on disk, one slice per worker."""

    @staticmethod
    def write(*, tasks: typing.List[dict], directory: pathlib.Path, slices: int) -> typing.List[pathlib.Path]:
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
        return written

    @staticmethod
    def write_instructions(*, directory: pathlib.Path, language: language_config.LanguageConfig) -> pathlib.Path:
        suffixes = examples.ExampleSet.translation_suffixes(language=language)
        path = directory / INSTRUCTIONS_FILE
        path.write_text(
            INSTRUCTIONS_TEMPLATE.format(
                target=language.target_name,
                languages=', '.join(f'`{suffix}` — {language.name_of(suffix=suffix)}' for suffix in suffixes),
                example_suffix=suffixes[0],
            ),
            encoding='utf-8',
        )
        return path

    @staticmethod
    def load_planned(*, directory: pathlib.Path) -> typing.Dict[str, typing.Dict[int, str]]:
        """Which sentence each line number stood for, as the task files recorded it.

        A key appears in exactly one slice, so reading them all builds one map with no conflict.
        """
        planned: typing.Dict[str, typing.Dict[int, str]] = {}
        for path in sorted(directory.glob(f'task-*{ANSWER_SUFFIX}')):
            payload = json.loads(path.read_text(encoding='utf-8'))
            for row in payload.get('rows', []):
                planned[row['key']] = {
                    sentence['line']: sentence['target']
                    for sentence in row.get('sentences', [])
                    if isinstance(sentence.get('line'), int)
                }
        return planned

    @staticmethod
    def load_answers(*, directory: pathlib.Path) -> typing.Dict[str, dict]:
        answers: typing.Dict[str, dict] = {}
        for path in sorted(directory.glob(f'*{ANSWER_SUFFIX}')):
            if path.name.startswith('task-'):
                continue
            payload = json.loads(path.read_text(encoding='utf-8'))
            for row in payload.get('rows', []):
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
    cards_path = data_directory / CARDS_FILE
    rows = language_config.TsvFile.read(cards_path)
    translation_plan = TranslationPlan(language=language)

    if plan:
        rows = translation_plan.reorder(rows=rows)
        language_config.TsvFile.write(path=cards_path, rows=rows, columns=language.card_columns)
        tasks = translation_plan.build(rows=rows)
        directory = data_directory / TASK_DIRECTORY
        written = TaskFiles.write(tasks=tasks, directory=directory, slices=slices)
        TaskFiles.write_instructions(directory=directory, language=language)
        gaps = sum(
            len(sentence['missing']) for task in tasks for sentence in task['sentences']
        ) + sum(len(task['definition']['missing']) for task in tasks if 'definition' in task)
        print(f"reordered {len(rows)} rows; {gaps} translations missing across {len(tasks)} words")
        print(f"wrote {len(written)} task files to {directory}")
        return

    if from_json is not None:
        answers = TaskFiles.load_answers(directory=from_json)
        # The task files are the only record of what a line number meant, so they are read from
        # where the plan wrote them rather than from wherever the answers were collected.
        planned = TaskFiles.load_planned(directory=data_directory / TASK_DIRECTORY)
        rows, filled, unresolved = translation_plan.merge(rows=rows, answers=answers, planned=planned)
        language_config.TsvFile.write(path=cards_path, rows=rows, columns=language.card_columns)
        print(f"filled {filled} translations from {len(answers)} answered words")
        if unresolved:
            print(
                f"{unresolved} answers named a sentence their task file no longer describes "
                f"and were skipped; re-run --plan and answer the new task files",
                file=sys.stderr,
            )
        return

    # Only reachable when a caller other than main() skips its choose-one validation.
    raise ValueError("choose plan=True or from_json")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', action='store_true', help="Reorder examples and write task files")
    parser.add_argument('--from-json', type=pathlib.Path, default=None, help="Directory holding answer files")
    parser.add_argument('--slices', type=int, default=SLICE_COUNT)
    language_config.add_language_arguments(parser)
    arguments = parser.parse_args()

    if not arguments.plan and arguments.from_json is None:
        parser.error("choose --plan or --from-json")

    root = language_config.PROJECT_ROOT
    language = language_config.language_from(arguments, root=root)
    main_for(language=language, root=root, plan=arguments.plan, from_json=arguments.from_json, slices=arguments.slices)


if __name__ == '__main__':
    main()
