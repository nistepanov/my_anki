"""How every task-file stage writes its questions, and the prose it hands its worker.

The stages differ in what they ask for, not in how the asking works: a directory of numbered
task files, one answer file each, answered by whoever is available. Rules about that mechanism
belong to the mechanism, and a copy per stage is a correction that lands in only one of them.
"""

import json
import pathlib
import typing
from datetime import datetime

TASK_PREFIX = 'task-'
ANSWER_PREFIX = 'answer-'
ARCHIVE_PREFIX = 'done-'
ARCHIVE_STAMP = '%Y-%m-%dT%H%M%S'


# Appended to every stage's instructions. The cost of losing a half-finished answer file is the
# whole file, and that has happened often enough to be worth a paragraph.
INCREMENTAL_SAVING = """
## Saving as you go

Write the answer file as you work rather than once at the end: every ten rows or so, rewrite it
whole with everything finished so far. Rewriting keeps it valid JSON at every moment, so a run
that stops early loses the rows in hand instead of all of them.

Read the answer file first if it already exists. The rows it names are done and must not be
answered again — start from the first row of the task file it does not mention, and keep the
rows already there exactly as they are.
"""


def archive_previous_round(*, directory: pathlib.Path) -> typing.Optional[pathlib.Path]:
    """Move the last round's task and answer files into a dated subdirectory.

    An answer left from an old round would merge again and undo a newer answer.
    """
    stale = sorted([*directory.glob(f'{TASK_PREFIX}*.json'), *directory.glob(f'{ANSWER_PREFIX}*.json')])
    if not stale:
        return None
    archive = directory / f'{ARCHIVE_PREFIX}{datetime.now().strftime(ARCHIVE_STAMP)}'
    archive.mkdir()
    for path in stale:
        path.rename(archive / path.name)
    print(f"moved {len(stale)} files of the previous round to {archive}")
    return archive


def write(
    *, rows: typing.List[dict], directory: pathlib.Path, slices: int, compact: bool = False,
) -> typing.List[pathlib.Path]:
    """Start a new round: archive the old one, then split the rows into numbered task files."""
    directory.mkdir(parents=True, exist_ok=True)
    archive_previous_round(directory=directory)
    size = -(-len(rows) // slices) if rows else 0
    layout = {'separators': (',', ':')} if compact else {'indent': 1}
    written = []
    for index in range(slices):
        chunk = rows[index * size:(index + 1) * size] if size else []
        if not chunk:
            continue
        path = directory / f'{TASK_PREFIX}{index + 1:02d}.json'
        path.write_text(json.dumps({'rows': chunk}, ensure_ascii=False, **layout), encoding='utf-8')
        written.append(path)
    return written
