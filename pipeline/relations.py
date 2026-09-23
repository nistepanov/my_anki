"""Make a word's synonyms and antonyms usable: sort them, and say what each one means.

A synonym list flattens two very different relations. `cabeza` and `caletre` are both the thing
on your shoulders; `cabeza` and `jefe` are the same word only because a head leads a body. A
learner who takes the second as an ordinary swap will write nonsense, and the list as published
gives no way to tell which is which — dictionaries mark it inside the sense text, not as data.

A third group turned up while sorting the first two: entries sharing no sense with the headword
at all. One list offers `siempre` as a synonym of `jamás`, its own opposite. These come of taking
a dictionary's related-words block at face value, where proximity in an entry is not
interchangeability. They are worse than the figurative ones — a figurative synonym teaches
something incomplete, a wrong one teaches something false — so they are dropped from the card
rather than labelled.

Nothing is deleted from the table. The published list stays as it came, and the judgements are
recorded beside it, so a bad call here can be found and reversed.

The lists also arrive as bare foreign words, which is close to useless on a card: being told that
`cara` is also `rostro` and `jeta` teaches nothing unless you already know those two. So each one
is glossed in the learner's own language, and the same is done for the antonyms, which have the
same problem and were not being sorted at all.

Two steps, like the other model-backed stages: `--plan` writes the task files, `--from-json`
merges the answers.
"""

import argparse
import json
import pathlib
import typing

from . import language_config
from . import tasks

CARDS_FILENAME = 'cards.tsv'
TASK_DIRECTORY = 'relations'
INSTRUCTIONS_FILE = 'INSTRUCTIONS.md'
ANSWER_SUFFIX = '.json'
SLICE_COUNT = 8

FIGURATIVE_COLUMN = 'figurative_synonyms'
REJECTED_COLUMN = 'rejected_synonyms'
SOURCE_COLUMN = 'relations_source'
SOURCE_NAME = 'model'
SYNONYM_SEPARATOR = ', '
RELATED_COLUMNS = ('synonyms', 'antonyms')
ANSWER_KEYS = ('figurative', 'wrong', 'glosses')

INSTRUCTIONS_TEMPLATE = """# Sorting synonyms by how they relate

Each task file lists words in {target} with the sense their card teaches and the synonyms
already on that card. For each word, say which of those synonyms match it only figuratively.

## Output

Write `answer-NN.json` next to the task file, `NN` matching the task file's number:

```json
{{"rows": [{{"key": "<copied verbatim>",
   "figurative": ["jefe"],
   "wrong": ["siempre"],
   "glosses": {{"jefe": "начальник", "caletre": "смекалка", "nunca": "никогда"}}}}]}}
```

One row per word in your task file, no omissions. `figurative` and `wrong` hold words **copied
verbatim from that row's own lists** — never a word that is not in them, never a rephrasing. A word
belongs to at most one of the two. Two empty lists is the common, correct answer.

`glosses` covers **every** word in the row's `synonyms` and `antonyms`, including the ones you
marked and the antonyms you were not asked to judge. Leave it out only when both lists are empty.

## What counts as figurative

A synonym is figurative when the two words share a sense only through an image: a head leads a
body, so `cabeza` can mean `jefe`; a heart is at the centre, so `corazón` can mean the middle of
a city. Swapping such a word into a sentence about the literal thing produces nonsense, which is
exactly what the learner needs warning about.

A synonym is **not** figurative when both words denote the same thing plainly, even if one is
formal, regional, colloquial or rarer — `rostro` and `faz` for `cara` are all literally a face,
and belong in the ordinary group.

Judge against `meaning`, which is the sense this card teaches. The same spelling on another card
may relate differently, and that is not your concern here.

A synonym that belongs to a **different sense of the headword** is not figurative either, and must
be left out. `piso` meaning a storey and `piso` meaning a floor surface are two senses of one word,
not an image of one another; `dos` and `segundo` are a cardinal and an ordinal. Listing these under
the figurative heading tells the learner they are a usable figure of speech, when in truth they
belong to a card this is not. Only an image connects the two words for a figurative synonym —
if you cannot name the image, the answer is no.

As a check on yourself: figurative synonyms are uncommon. Marking more than about one row in five
means the test has drifted, and what is being caught is polysemy or plain error instead.

## What counts as wrong

`wrong` is for a listed synonym that shares no sense with the headword whatever — not a figure of
speech, not another sense of the same word, simply not a synonym. `siempre` for `jamás` is the
clearest kind: the exact opposite. Others come from a shared root or a lookalike spelling —
`prender` for `aprender`, `matasellar` for `matar`.

The card drops these, so the test is strict: list a word here only when you are confident no
reading of the headword makes them interchangeable. If it might be a rare or regional sense you do
not know, leave it alone — a doubtful synonym kept is a smaller error than a good one deleted.

A synonym belonging to a different sense of the headword is **not** wrong; it is right on another
card. Leave those out of both lists.

## Writing a gloss

One or two words in {native}, naming what the {target} word means — the sense that makes it a
relative of this headword, not the word's most famous sense elsewhere. No part-of-speech labels,
no brackets, no explanation: the gloss sits inline on a card beside the word it explains and has
room for nothing more.

Where a synonym is figurative, gloss the figurative sense, since that is the one being flagged.
Where it is wrong, gloss it anyway — the gloss is what will let a reader see the mistake.

A word you cannot honestly translate as a synonym goes in `wrong`. Never leave it in the list
with a note in place of its meaning — "(не связано)", "бессмыслица", "нет такого слова" — because
the card shows the word with that note as its translation, and the learner reads the apology as
the meaning.


## Verifying

Before finishing, check with Python that the file parses, every `key` matches your task file
verbatim, every row is answered, every word in `figurative` and `wrong` appears in that row's own
synonym list, and every word across the row's synonyms and antonyms has a gloss. Report those
counts.
""" + tasks.INCREMENTAL_SAVING


class RelationPlan:
    """The words whose synonym lists have not been sorted yet."""

    def __init__(self, *, language: language_config.LanguageConfig):
        self._language = language

    @staticmethod
    def words_in(*, row: dict, column: str) -> typing.List[str]:
        return [part.strip() for part in row.get(column, '').split(',') if part.strip()]

    @classmethod
    def synonyms_of(cls, *, row: dict) -> typing.List[str]:
        return cls.words_in(row=row, column='synonyms')

    @classmethod
    def related_to(cls, *, row: dict) -> typing.List[str]:
        """Every word the card shows beside the headword, whichever list it came from."""
        return [word for column in RELATED_COLUMNS for word in cls.words_in(row=row, column=column)]

    def build(self, *, rows: typing.List[dict]) -> typing.List[dict]:
        return [
            {
                'key': row['key'],
                'word': row.get('word', ''),
                'meaning': row.get(self._language.translations_native_column, ''),
                'definition': row.get(self._language.definition_column, ''),
                'synonyms': self.synonyms_of(row=row),
                'antonyms': self.words_in(row=row, column='antonyms'),
            }
            for row in rows
            if self.related_to(row=row) and not self.is_settled(row=row)
        ]

    def is_settled(self, *, row: dict) -> bool:
        """Ask the table, not the answer files.

        Answer files are rotated: each re-plan writes a different set of words into the same
        numbered files, so an answer that settled a word is gone by the next round even though
        its result is safely in the table. Judging by the answers therefore re-asks questions
        that were already answered — hundreds of them. The table is what the work is for, so the
        table is what decides.

        Completeness of the glosses is the test. The two verdicts cannot be tested this way,
        because an empty verdict is the common correct answer and reads the same as a missing
        one — but a pass that glossed a word also judged it, so the glosses stand for both.
        """
        for column in RELATED_COLUMNS:
            words = self.words_in(row=row, column=column)
            glosses = self.words_in(row=row, column=f'{column}_{self._language.native}')
            if words and len(glosses) != len(words):
                return False
        return True

    def merge(
        self, *, rows: typing.List[dict], answers: typing.Dict[str, dict],
    ) -> typing.Tuple[typing.List[dict], int, int, int]:
        marked = dropped = glossed = 0
        merged = []
        for row in rows:
            answer = answers.get(row['key'])
            if answer is None:
                merged.append(row)
                continue
            # Only words actually on the row's own list may be marked, so a stray answer cannot
            # invent a synonym the card never showed.
            allowed = {word.lower(): word for word in self.synonyms_of(row=row)}
            figurative = self._known(words=answer.get('figurative', []), allowed=allowed)
            rejected = self._known(words=answer.get('wrong', []), allowed=allowed)
            # A word claimed as both is kept as merely figurative: the milder judgement is the
            # one that keeps it on the card, and a contradiction is not grounds for deleting.
            rejected = [word for word in rejected if word not in figurative]
            updated = dict(row)
            updated[FIGURATIVE_COLUMN] = SYNONYM_SEPARATOR.join(figurative)
            updated[REJECTED_COLUMN] = SYNONYM_SEPARATOR.join(rejected)
            glossed += self._apply_glosses(row=updated, answer=answer)
            updated[SOURCE_COLUMN] = SOURCE_NAME
            marked += len(figurative)
            dropped += len(rejected)
            merged.append(updated)
        return merged, marked, dropped, glossed

    def _apply_glosses(self, *, row: dict, answer: dict) -> int:
        """Write each list's glosses as a parallel column, aligned word for word.

        Aligning by position rather than pairing inline keeps the published list untouched and
        lets the card decide how to show the two together — the same arrangement the example
        sentences already use.
        """
        glosses = {str(word).strip().lower(): self._single_field(gloss=str(gloss))
                   for word, gloss in (answer.get('glosses') or {}).items()}
        if not glosses:
            return 0
        written = 0
        for column in RELATED_COLUMNS:
            words = self.words_in(row=row, column=column)
            if not words:
                continue
            target = f'{column}_{self._language.native}'
            # An answer that says nothing about a word must not erase what an earlier pass said
            # about it: writing the column unconditionally turns a gloss into an empty string,
            # and the cell still looks filled because the separators remain.
            existing = self.words_in(row=row, column=target)
            kept = existing if len(existing) == len(words) else [''] * len(words)
            translated = [glosses.get(word.lower()) or kept[index] for index, word in enumerate(words)]
            if not any(translated):
                continue
            row[target] = SYNONYM_SEPARATOR.join(translated)
            written += sum(1 for gloss in translated if gloss)
        return written

    @staticmethod
    def _single_field(*, gloss: str) -> str:
        """A gloss with its commas defused, so it stays one element of the parallel list.

        The list separator also reads as ordinary punctuation, and a gloss that carries it splits
        into two on the way to the card, shifting every word after it onto the wrong translation.
        Rewriting the punctuation keeps the meaning and the alignment both; dropping the gloss
        instead, as this once did, left the word bare on the card for no gain.
        """
        return gloss.strip().replace(',', ';')

    @staticmethod
    def _known(*, words: typing.Sequence[str], allowed: typing.Dict[str, str]) -> typing.List[str]:
        return [allowed[str(word).strip().lower()] for word in words if str(word).strip().lower() in allowed]


class TaskFiles:
    """Task and answer files on disk, one slice per worker."""

    @staticmethod
    def write(*, tasks: typing.List[dict], directory: pathlib.Path, slices: int,
              language: language_config.LanguageConfig) -> typing.List[pathlib.Path]:
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
        # Archived passes live in subdirectories and are read first, so an answer from the
        # current pass overrides them rather than the other way round.
        archived = sorted(path for path in directory.rglob(f'answer-*{ANSWER_SUFFIX}') if path.parent != directory)
        current = sorted(directory.glob(f'answer-*{ANSWER_SUFFIX}'))
        for path in (*archived, *current):
            for row in json.loads(path.read_text(encoding='utf-8')).get('rows', []):
                # Merge rather than replace, so a later pass answering one more question about a
                # word does not erase what an earlier pass established about it.
                answers.setdefault(row['key'], {}).update(row)
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
    relations = RelationPlan(language=language)

    if plan:
        directory = data_directory / TASK_DIRECTORY
        tasks = relations.build(rows=rows)
        if not tasks:
            print("every word with synonyms has been sorted")
            return
        written = TaskFiles.write(
            tasks=tasks, directory=directory, slices=slices, language=language,
        )
        print(f"{len(tasks)} words need sorting; wrote {len(written)} task files to {directory}")
        return

    if from_json is not None:
        answers = TaskFiles.load_answers(directory=from_json)
        rows, marked, dropped, glossed = relations.merge(rows=rows, answers=answers)
        language_config.TsvFile.write(cards_path, rows=rows, columns=language.card_columns)
        print(
            f"across {len(answers)} answered words: {marked} figurative synonyms, "
            f"{dropped} dropped as not synonyms at all, {glossed} words glossed"
        )
        return

    raise SystemExit("choose --plan or --from-json")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', action='store_true', help="Write task files for unsorted words")
    parser.add_argument('--from-json', type=pathlib.Path, default=None, help="Directory holding answers")
    parser.add_argument('--slices', type=int, default=SLICE_COUNT)
    language_config.add_language_arguments(parser)
    arguments = parser.parse_args()

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
