"""Fill the fields no dictionary exposes, and simplify the ones that are too dense.

Dictionaries supply the facts; the model only restates a dictionary definition in A2-level
target-language wording, writes example sentences where no corpus had any, and settles the few
questions nothing published answers.

Every question that can be looked up instead is looked up. A published graded word list states
a level better than a model infers one, and a part of speech rules out a picture without anyone
judging anything, so a word is asked about those two only where no source already settles them.

Language-neutral: the target, native and pivot languages come from a config under languages/.
"""

import argparse
import concurrent.futures
import hashlib
import json
import pathlib
import sys
import typing

from . import graded_lexicon
from . import language_config
from . import tasks

MODEL = 'claude-sonnet-5'
BATCH_SIZE = 20
MAX_WORKERS = 4
MAX_TOKENS = 16000
EFFORT = 'low'
# Bump when the prompt or schema changes, so cached answers are not reused across revisions.
PROMPT_VERSION = 'v3'

CEFR_LEVELS = ('A1', 'A2', 'B1', 'B2', 'C1', 'C2')
# The only part of speech that can name something a picture could show.
NOUN = 'noun'
EXAMPLES_PER_WORD = 3
RELATED_WORDS_LIMIT = 3
# Marks a card whose related words belong to the sense it teaches, so later passes leave them be.
RELATIONS_SOURCE = 'chosen sense'

TASK_DIRECTORY = 'llm'
INSTRUCTIONS_FILE = 'INSTRUCTIONS.md'
TASK_PREFIX = 'task-'
SLICE_COUNT = 8
INPUT_FILENAME = 'enriched.tsv'
OUTPUT_FILENAME = 'cards.tsv'
CACHE_DIRECTORY = 'cache'

SYSTEM_PROMPT_TEMPLATE = """You are a lexicographer building {target} flashcards for a {native}-speaking learner.

For every word you are given, produce:

definition — one sentence in {target} using A2-level vocabulary, defining the sense that matches the {native} translation supplied. Never use the word itself or any word sharing its root. When a dictionary definition is supplied it is authoritative on meaning: restate it more simply, do not replace its sense.

translation — only when the needs list asks for it. The word's meaning in {native}, as a dictionary would gloss it: one to three words, comma separated, commonest sense first. No explanation, no brackets, no part-of-speech label.

part_of_speech — only when the needs list asks for it. One of: noun, verb, adjective, adverb, pronoun, preposition, conjunction, numeral, interjection, article.

cefr — only when the needs list asks for it. The level at which a learner normally meets this word: A1, A2, B1, B2, C1 or C2. The frequency band is a strong hint, not a rule; a rare word inside a common topic can still be early.

is_object — only when the needs list asks for it. True when the word names something physical that could be photographed: a table, a policeman, a city, a tooth. False for actions, qualities, feelings, relations and abstractions.

examples — only when the word's needs list asks for them. Three sentences of four to eight words in {target}, present or simple past tense, everyday vocabulary, each showing the sense defined above, each with a natural {native} translation.

synonyms and antonyms — only when the needs list asks for them. Up to three {target} words each, same part of speech as the headword. Return an empty list when the word genuinely has none; never pad.

Return one entry per id you were given, in the order received."""

# Wraps the system prompt above with the file protocol a plan worker needs; the field rules
# themselves live only in SYSTEM_PROMPT_TEMPLATE.
PLAN_INSTRUCTIONS_TEMPLATE = """# Enriching vocabulary rows

""" + SYSTEM_PROMPT_TEMPLATE + """

## Output

Read one `task-NN.json` from this directory — its `rows` array holds the requests described
above, each carrying the id and a `needs` list naming which optional fields it wants. Write the
answer as `answer-NN.json` in the same directory, `NN` matching the task file's number. It must
parse with `json.load`: UTF-8, no markdown fence, no commentary.

```json
{{"rows": [
  {{"id": "<copied verbatim from the task>",
   "translation": "...", "part_of_speech": "noun",
   "definition": "...", "cefr": "A1", "is_object": true,
   "examples": [{{"target": "...", "native": "..."}}],
   "synonyms": [], "antonyms": []}}
]}}
```

Answer every row from the task file, one object each, matched by `id` — never by position; an
omitted id leaves that word unenriched. Fill `cefr`, `is_object`, `examples`, `synonyms` and `antonyms` only for the
fields a row's `needs` list names; otherwise return them as empty lists, even when you know a
plausible value.


## Verifying

Before finishing, check with Python that the file parses, every `id` appears in the task file,
every `cefr` you gave is one of {cefr_levels}, every `is_object` is a real boolean, and no row carries a field
its `needs` list did not ask for. Report those counts.
""" + tasks.INCREMENTAL_SAVING

ENRICHMENT_SCHEMA = {
    'type': 'object',
    'properties': {
        'words': {
            'type': 'array',
            'items': {
                'type': 'object',
                'properties': {
                    'id': {'type': 'string'},
                    'translation': {'type': 'string'},
                    'part_of_speech': {'type': 'string'},
                    'definition': {'type': 'string'},
                    'cefr': {'type': 'string', 'enum': list(CEFR_LEVELS)},
                    'is_object': {'type': 'boolean'},
                    'examples': {
                        'type': 'array',
                        'items': {
                            'type': 'object',
                            'properties': {
                                'target': {'type': 'string'},
                                'native': {'type': 'string'},
                            },
                            'required': ['target', 'native'],
                            'additionalProperties': False,
                        },
                    },
                    'synonyms': {'type': 'array', 'items': {'type': 'string'}},
                    'antonyms': {'type': 'array', 'items': {'type': 'string'}},
                },
                'required': ['id', 'translation', 'part_of_speech', 'definition',
                             'examples', 'synonyms', 'antonyms'],
                'additionalProperties': False,
            },
        },
    },
    'required': ['words'],
    'additionalProperties': False,
}


class AnswerFiles:
    """Answers produced outside the paid API — one JSON file per batch, any file names."""

    @staticmethod
    def load(*, directory: pathlib.Path) -> typing.Dict[str, dict]:
        answers: typing.Dict[str, dict] = {}
        for path in sorted(directory.glob('*.json')):
            # The answers usually sit beside the task files they answer, and a task row carries
            # the same id as its answer with none of the fields — read as an answer it would
            # blank the row it was asking about.
            if path.name.startswith(TASK_PREFIX):
                continue
            payload = json.loads(path.read_text(encoding='utf-8'))
            if isinstance(payload, list):
                entries = payload
            else:
                entries = payload.get('words') or payload.get('rows') or []
            for entry in entries:
                if 'id' in entry:
                    answers[str(entry['id'])] = entry
        return answers


class Enricher:
    """Batches rows into model requests and merges the answers back."""

    def __init__(
        self,
        *,
        client,
        language: language_config.LanguageConfig,
        cache_directory: pathlib.Path,
        lexicon: typing.Optional[graded_lexicon.GradedLexicon] = None,
    ):
        self._client = client
        self._language = language
        self._cache_directory = cache_directory
        self._cache_directory.mkdir(parents=True, exist_ok=True)
        self._lexicon = lexicon

    def published_level(self, *, row: dict) -> str:
        """The level a graded word list gives this word, if one covers it.

        Asking a model to judge a level it can simply be told is the clearest waste in this
        stage, and the answer is worse: a level someone assigned by looking at the word is a
        verdict, one a model infers from frequency is a guess. The lists cover every word the
        deck now grows by, because that is where the new words came from.
        """
        if self._lexicon is None:
            return ''
        found = self._lexicon.level_of(
            word=row['word'],
            part_of_speech=graded_lexicon.first_part_of_speech(value=row.get('part_of_speech', '')),
        )
        return found.level.upper() if found is not None else ''

    def merge_from_answers(self, *, rows: typing.List[dict], answers: typing.Dict[str, dict]) -> typing.List[dict]:
        return [self.merge(row=row, answer=answers.get(row['key'])) for row in rows]

    def is_answered(self, *, row: dict, answer: typing.Optional[dict]) -> bool:
        """An answer counts only when it carries every field the row asked for."""
        if answer is None:
            return False
        needed = self.build_request(row=row)['needs']
        return all(str(answer.get(name, '')).strip() or answer.get(name) for name in needed)

    @staticmethod
    def keep_finished(*, existing: typing.List[dict], rebuilt: typing.List[dict]) -> typing.List[dict]:
        """Merge cell by cell: what the table already holds wins, gaps are filled from the rebuild.

        This stage reads the pre-model table, so overwriting a row here reverts everything the
        stages after it wrote — example ordering above all, which later answers address by line
        number. Keeping the whole existing row instead is too blunt in the other direction: it
        also blocks a field this stage only learned to produce later from ever reaching a row
        that predates it. Filling only the empty cells satisfies both.
        """
        finished = {row['key']: row for row in existing}
        merged = []
        for row in rebuilt:
            known = finished.get(row['key'])
            if known is None:
                merged.append(row)
                continue
            updated = dict(known)
            for column, value in row.items():
                if value and not updated.get(column, '').strip():
                    updated[column] = value
            merged.append(updated)
        return merged

    @staticmethod
    def drop_unanswered(
        *, rows: typing.List[dict], answers: typing.Dict[str, dict], existing: typing.List[dict],
    ) -> typing.List[dict]:
        """Carry only the rows the model has actually seen, plus the ones already finished.

        A row written out before it has an answer is indistinguishable, next time, from a row that
        is done: the stage keeps whatever the table already holds, so an empty row written early
        stays empty forever and every later answer for it is silently ignored. Growing the table
        as the answers arrive keeps that from happening, and keeps a half-enriched word off a card.
        """
        finished = {row['key'] for row in existing}
        return [row for row in rows if row['key'] in answers or row['key'] in finished]

    def run(self, *, rows: typing.List[dict]) -> typing.List[dict]:
        batches = [rows[start:start + BATCH_SIZE] for start in range(0, len(rows), BATCH_SIZE)]
        answers: typing.Dict[str, dict] = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {pool.submit(self._enrich_batch, batch): index for index, batch in enumerate(batches)}
            for done, future in enumerate(concurrent.futures.as_completed(futures), start=1):
                try:
                    answers.update(future.result())
                except Exception as error:  # one bad batch must not lose the rest
                    print(f"batch {futures[future]} failed: {error}", file=sys.stderr)
                else:
                    print(f"batch {done}/{len(batches)} done", file=sys.stderr)
        return [self.merge(row=row, answer=answers.get(row['key'])) for row in rows]

    def _enrich_batch(self, batch: typing.List[dict]) -> typing.Dict[str, dict]:
        payload = [self.build_request(row=row) for row in batch]
        cache_path = self._cache_directory / f'{self.cache_key(payload=payload)}.json'
        if cache_path.exists():
            words = json.loads(cache_path.read_text(encoding='utf-8'))['words']
        else:
            response = self._client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=[{
                    'type': 'text',
                    'text': SYSTEM_PROMPT_TEMPLATE.format(
                        target=self._language.target_name,
                        native=self._language.native_name,
                    ),
                    'cache_control': {'type': 'ephemeral'},
                }],
                thinking={'type': 'adaptive'},
                output_config={
                    'effort': EFFORT,
                    'format': {'type': 'json_schema', 'schema': ENRICHMENT_SCHEMA},
                },
                messages=[{'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)}],
            )
            text = next(block.text for block in response.content if block.type == 'text')
            words = json.loads(text)['words']
            cache_path.write_text(json.dumps({'words': words}, ensure_ascii=False), encoding='utf-8')
        return {str(word['id']): word for word in words}

    @staticmethod
    def _names_a_thing_or_might(*, row: dict) -> bool:
        """Whether the word could possibly name something a camera can point at.

        A word whose part of speech is not yet known counts, because the answer that settles it
        arrives in the same reply.
        """
        part_of_speech = row.get('part_of_speech', '').strip().lower()
        return not part_of_speech or part_of_speech.split()[0] == NOUN

    def build_request(self, *, row: dict) -> dict:
        language = self._language
        needs = []
        if not row.get(language.translations_native_column):
            needs.append('translation')
        if not row.get('part_of_speech'):
            needs.append('part_of_speech')
        if not row.get(f'examples_{language.target}'):
            needs.append('examples')
        if not row.get('synonyms'):
            needs.append('synonyms')
        if not row.get('antonyms'):
            needs.append('antonyms')
        if not row.get('cefr') and not self.published_level(row=row):
            needs.append('cefr')
        # Only a noun can name something photographable, and that is settled by looking at the
        # part of speech rather than by asking. Among nouns it is a real judgement — half of
        # them name something abstract — so those are still asked.
        if self._names_a_thing_or_might(row=row):
            needs.append('is_object')
        request = {
            'id': row['key'],
            'word': ' '.join(part for part in (row.get('article'), row['word']) if part),
            'part_of_speech': row.get('part_of_speech', ''),
            'translation': row.get(f'translations_{language.native}', ''),
            'frequency_band': row.get('frequency_band', ''),
            'needs': needs,
        }
        if row.get(language.definition_column):
            request['dictionary_definition'] = row[language.definition_column]
        return request

    @staticmethod
    def cache_key(*, payload: typing.List[dict]) -> str:
        material = PROMPT_VERSION + json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(material.encode('utf-8')).hexdigest()[:16]

    def merge(self, *, row: dict, answer: typing.Optional[dict]) -> dict:
        language = self._language
        merged = dict(row)
        merged.setdefault(language.definition_full_column, '')
        if answer is None:
            merged['tags'] = self.build_tags(row=merged)
            return merged

        # Keep the dictionary wording; the model's simplification is what the card shows.
        merged[language.definition_full_column] = row.get(language.definition_column, '')
        merged[language.definition_column] = answer['definition']
        # A word that arrived from a graded lexicon has nothing but its spelling, so these two
        # are filled here or not at all; a word that came with them keeps what it came with.
        for column, name in (
            (language.translations_native_column, 'translation'),
            ('part_of_speech', 'part_of_speech'),
        ):
            if not merged.get(column) and answer.get(name):
                merged[column] = answer[name]
        # A published level is authoritative; the model is only asked to judge one where no
        # list covers the word.
        merged['cefr'] = row.get('cefr') or self.published_level(row=merged) or answer.get('cefr', '')
        # Absent means the question was never put, which happens only where the part of
        # speech already rules a picture out.
        merged['is_object'] = 'yes' if answer.get('is_object') else 'no'

        if not row.get(f'examples_{language.target}') and answer['examples']:
            examples = answer['examples'][:EXAMPLES_PER_WORD]
            merged[f'examples_{language.target}'] = '<br>'.join(
                example['target'] for example in examples
            )
            merged[f'examples_{language.native}'] = '<br>'.join(
                example['native'] for example in examples
            )
            if language.pivot is not None:
                # Keep the columns index-aligned even though the model was not asked for a pivot translation.
                merged[f'examples_{language.pivot}'] = '<br>' * (len(examples) - 1)
            merged['examples_source'] = 'llm'
        if not row.get('synonyms'):
            merged['synonyms'] = ', '.join(answer['synonyms'][:RELATED_WORDS_LIMIT])
        if not row.get('antonyms'):
            merged['antonyms'] = ', '.join(answer['antonyms'][:RELATED_WORDS_LIMIT])

        merged['relations_source'] = merged.get('relations_source') or RELATIONS_SOURCE
        merged['tags'] = self.build_tags(row=merged)
        return merged

    @staticmethod
    def build_tags(*, row: dict) -> str:
        """Anki splits tags on whitespace, so every tag has to be a single token."""
        tags = list(row.get('part_of_speech', '').split())
        if row.get('gender'):
            tags.append(f'gender::{row["gender"]}')
        if row.get('number'):
            tags.append(row['number'])
        if row.get('cefr'):
            tags.append(f'cefr::{row["cefr"]}')
        if row.get('frequency_band'):
            tags.append(f'freq::{row["frequency_band"]}')
        if row.get('is_object'):
            tags.append('object' if row['is_object'] == 'yes' else 'abstract')
        tags += [f'topic::{category}' for category in row.get('categories', '').split()]
        if row.get('status') == 'studied':
            tags.append('studied')
        return ' '.join(tags)


class TaskFiles:
    """Task files for the paid model stage, one slice per worker."""

    @staticmethod
    def write(*, payloads: typing.List[dict], directory: pathlib.Path, slices: int) -> typing.List[pathlib.Path]:
        directory.mkdir(parents=True, exist_ok=True)
        size = -(-len(payloads) // slices) if payloads else 0
        written = []
        for index in range(slices):
            chunk = payloads[index * size:(index + 1) * size] if size else []
            if not chunk:
                continue
            path = directory / f'{TASK_PREFIX}{index + 1:02d}.json'
            path.write_text(json.dumps({'rows': chunk}, ensure_ascii=False, indent=1), encoding='utf-8')
            written.append(path)
        return written

    @staticmethod
    def write_instructions(*, directory: pathlib.Path, language: language_config.LanguageConfig) -> pathlib.Path:
        path = directory / INSTRUCTIONS_FILE
        path.write_text(
            PLAN_INSTRUCTIONS_TEMPLATE.format(
                target=language.target_name,
                native=language.native_name,
                cefr_levels=', '.join(CEFR_LEVELS),
            ),
            encoding='utf-8',
        )
        return path


def main_for(
    *,
    language: language_config.LanguageConfig,
    root: pathlib.Path,
    plan: bool = False,
    from_json: typing.Optional[pathlib.Path] = None,
    refresh: bool = False,
    slices: int = SLICE_COUNT,
    limit: typing.Optional[int] = None,
    input_path: typing.Optional[pathlib.Path] = None,
    output_path: typing.Optional[pathlib.Path] = None,
) -> None:
    data_directory = language.data_directory(root=root)
    input_path = input_path if input_path is not None else data_directory / INPUT_FILENAME
    output_path = output_path if output_path is not None else data_directory / OUTPUT_FILENAME

    rows = language_config.TsvFile.read(input_path)
    if limit is not None:
        rows = rows[:limit]
    existing = language_config.TsvFile.read(output_path) if output_path.exists() else []

    lexicon = graded_lexicon.GradedLexicon.load(language=language, data_directory=data_directory)
    enricher = Enricher(
        client=None,
        language=language,
        cache_directory=data_directory / CACHE_DIRECTORY / TASK_DIRECTORY,
        lexicon=lexicon,
    )
    if plan:
        # The input table is the pre-model snapshot and never carries an answer, so what is
        # already done has to be read from the answers themselves.
        directory = data_directory / TASK_DIRECTORY
        answered = AnswerFiles.load(directory=directory) if directory.exists() else {}
        # An answer given before a field existed is not a finished answer: the row still needs
        # what its own needs list asks for, so a question added later re-opens it.
        selected = [
            row for row in rows
            if not enricher.is_answered(row=row, answer=answered.get(row['key']))
        ]
        if not selected:
            print("no rows need the model")
            return
        payloads = [enricher.build_request(row=row) for row in selected]
        written = TaskFiles.write(payloads=payloads, directory=directory, slices=slices)
        TaskFiles.write_instructions(directory=directory, language=language)
        print(f"{len(selected)} rows need the model")
        print(f"wrote {len(written)} task files to {directory}")
        return

    if from_json is not None:
        answers = AnswerFiles.load(directory=from_json)
        print(f"loaded {len(answers)} answers from {from_json}")
        enriched = enricher.merge_from_answers(rows=rows, answers=answers)
        if not refresh:
            enriched = Enricher.keep_finished(existing=existing, rebuilt=enriched)
            enriched = Enricher.drop_unanswered(rows=enriched, answers=answers, existing=existing)
    else:
        import anthropic

        enricher = Enricher(
            client=anthropic.Anthropic(),
            language=language,
            cache_directory=data_directory / CACHE_DIRECTORY / TASK_DIRECTORY,
            lexicon=lexicon,
        )
        enriched = enricher.run(rows=rows)
    language_config.TsvFile.write(output_path, rows=enriched, columns=language.card_columns)

    with_cefr = sum(1 for row in enriched if row.get('cefr'))
    objects = sum(1 for row in enriched if row.get('is_object') == 'yes')
    print(f"wrote {len(enriched)} rows to {output_path}")
    print(f"cefr {with_cefr} | objects {objects} | rows without answers {len(enriched) - with_cefr}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', nargs='?', type=pathlib.Path, default=None)
    parser.add_argument('output', nargs='?', type=pathlib.Path, default=None)
    parser.add_argument('--limit', type=int, default=None, help="Process only the first N rows")
    parser.add_argument('--plan', action='store_true', help="Write task files for rows the model still needs to see")
    parser.add_argument(
        '--from-json',
        type=pathlib.Path,
        default=None,
        help="Merge answers already written as JSON instead of calling the paid API",
    )
    parser.add_argument('--slices', type=int, default=SLICE_COUNT)
    parser.add_argument(
        '--refresh', action='store_true',
        help="Rebuild every row from the answers, discarding what later stages added",
    )
    language_config.add_language_arguments(parser)
    arguments = parser.parse_args()

    root = language_config.PROJECT_ROOT
    main_for(
        language=language_config.language_from(arguments, root=root),
        root=root,
        plan=arguments.plan,
        from_json=arguments.from_json,
        refresh=arguments.refresh,
        slices=arguments.slices,
        limit=arguments.limit,
        input_path=arguments.input,
        output_path=arguments.output,
    )


if __name__ == '__main__':
    main()
