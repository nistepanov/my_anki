"""Answer a stage's task files by calling the model API, instead of by hand or through an agent.

Every model-backed stage writes the same thing: a directory holding `INSTRUCTIONS.md` and
numbered `task-NN.json` files, each expecting an `answer-NN.json` beside it. Nothing in that
protocol says who does the answering, so one runner serves all of them and no stage changes.

Answering through the API rather than an agent is cheaper for the same work: the instructions
are identical across every request and are cached instead of re-read, the task rows are the only
thing that varies, and nothing pays for tooling it never uses. Batch submission halves the price
again, which suits a stage nobody waits on.

Work is saved as it is produced. A task file is answered in chunks, and the answer file is
rewritten after every chunk, so an interrupted run loses one chunk rather than a file. Re-running
resumes: rows an answer file already covers are not asked for a second time.
"""

import argparse
import json
import pathlib
import re
import sys
import time
import typing
from datetime import timedelta

import anthropic

DEFAULT_MODEL = 'claude-sonnet-5'
# Small enough that one chunk lost to an interruption is cheap to redo, large enough that the
# cached instructions are not the bulk of what each request pays for.
DEFAULT_CHUNK_SIZE = 20
MAX_OUTPUT_TOKENS = 16000
MAX_ATTEMPTS = 3
RETRY_PAUSE_SECONDS = timedelta(seconds=5).total_seconds()
BATCH_POLL_SECONDS = timedelta(seconds=30).total_seconds()
BATCH_ENDED_STATUS = 'ended'

INSTRUCTIONS_FILE = 'INSTRUCTIONS.md'
TASK_PREFIX = 'task-'
ANSWER_PREFIX = 'answer-'
ROWS_KEY = 'rows'
# Stages name a row either way, and a runner that serves all of them cannot insist on one.
ROW_IDENTIFIERS = ('key', 'id')

FENCE_PATTERN = re.compile(r'\A```[\w]*\s*|\s*```\Z')
TASK_NUMBER_PATTERN = re.compile(rf'\A{TASK_PREFIX}(\d+)\.json\Z')

# Said once, because the instructions are written for a reader who can run Python and check.
OUTPUT_REMINDER = (
    "Answer with the JSON object the instructions describe and nothing else — no markdown fence, "
    "no commentary before or after it. Cover every row you were given, in the order you were "
    "given them."
)


class MalformedAnswer(ValueError):
    """The model replied with something that is not the answer object the stage expects."""


def identify(*, row: dict) -> typing.Optional[str]:
    """What a row calls itself, under whichever of the identifier names its stage uses."""
    for name in ROW_IDENTIFIERS:
        value = row.get(name)
        if value is not None:
            return str(value)
    return None


class TaskFile(typing.NamedTuple):
    """One `task-NN.json` and the answer file that belongs to it."""

    path: pathlib.Path
    number: int
    rows: typing.List[dict]

    @classmethod
    def discover(cls, *, directory: pathlib.Path) -> typing.List['TaskFile']:
        found = []
        for path in sorted(directory.iterdir()):
            match = TASK_NUMBER_PATTERN.match(path.name)
            if match is None:
                continue
            payload = json.loads(path.read_text(encoding='utf-8'))
            found.append(cls(path=path, number=int(match.group(1)), rows=payload.get(ROWS_KEY, [])))
        return found

    @property
    def answer_path(self) -> pathlib.Path:
        return self.path.with_name(f'{ANSWER_PREFIX}{self.number:02d}.json')


class AnswerFile:
    """An answer file held open across chunks, rewritten whole every time it grows.

    Rewriting rather than appending keeps the file valid JSON at every moment, which is what
    makes an interrupted run resumable instead of merely recoverable.
    """

    def __init__(self, *, path: pathlib.Path):
        self._path = path
        self._rows: typing.Dict[str, dict] = {}
        if path.exists():
            payload = json.loads(path.read_text(encoding='utf-8'))
            for row in payload.get(ROWS_KEY, []):
                identifier = identify(row=row)
                if identifier is not None:
                    self._rows[identifier] = row

    @property
    def answered(self) -> typing.AbstractSet[str]:
        return self._rows.keys()

    def add(self, *, rows: typing.Iterable[dict]) -> int:
        added = 0
        for row in rows:
            identifier = identify(row=row)
            if identifier is None:
                continue
            self._rows[identifier] = row
            added += 1
        return added

    def save(self) -> None:
        payload = {ROWS_KEY: list(self._rows.values())}
        self._path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding='utf-8')


class Request(typing.NamedTuple):
    """One chunk of one task file, and where its answer belongs."""

    task_number: int
    index: int
    rows: typing.List[dict]

    @property
    def custom_id(self) -> str:
        return f'task{self.task_number:02d}-chunk{self.index:03d}'


class AnswerParser:
    """Reads the model's reply as the answer object the stage asked for."""

    @staticmethod
    def parse(*, text: str, expected: typing.AbstractSet[str]) -> typing.List[dict]:
        stripped = FENCE_PATTERN.sub('', text.strip())
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError as error:
            raise MalformedAnswer(f"reply is not JSON: {error}") from error
        rows = payload.get(ROWS_KEY) if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            raise MalformedAnswer("reply carries no rows array")
        # A row the task never mentioned is invention, and merging it would put a made-up word
        # on a card; a row that is merely missing is asked for again on the next run.
        kept = [row for row in rows if isinstance(row, dict) and identify(row=row) in expected]
        if not kept:
            raise MalformedAnswer("reply names none of the rows it was given")
        return kept


class TaskRunner:
    """Answers the task files in one directory, one stage's worth of work."""

    def __init__(self, *, directory: pathlib.Path, model: str, chunk_size: int):
        self._directory = directory
        self._model = model
        self._chunk_size = chunk_size
        self._client = anthropic.Anthropic()
        instructions_path = directory / INSTRUCTIONS_FILE
        if not instructions_path.exists():
            raise FileNotFoundError(f"{instructions_path} is missing; run the stage's --plan first")
        self._instructions = instructions_path.read_text(encoding='utf-8')

    def pending(self) -> typing.Tuple[typing.List[Request], typing.Dict[int, AnswerFile]]:
        """The chunks still unanswered, and the answer file each of them belongs to."""
        requests: typing.List[Request] = []
        answers: typing.Dict[int, AnswerFile] = {}
        for task in TaskFile.discover(directory=self._directory):
            answer = AnswerFile(path=task.answer_path)
            answers[task.number] = answer
            outstanding = [row for row in task.rows if identify(row=row) not in answer.answered]
            for index in range(0, len(outstanding), self._chunk_size):
                requests.append(Request(
                    task_number=task.number,
                    index=index // self._chunk_size,
                    rows=outstanding[index:index + self._chunk_size],
                ))
        return requests, answers

    def _parameters(self, *, rows: typing.List[dict]) -> dict:
        return {
            'model': self._model,
            'max_tokens': MAX_OUTPUT_TOKENS,
            'system': [{
                'type': 'text',
                'text': f'{self._instructions}\n\n{OUTPUT_REMINDER}',
                # Identical on every request of every chunk, so it is paid for once.
                'cache_control': {'type': 'ephemeral'},
            }],
            'messages': [{
                'role': 'user',
                'content': json.dumps({ROWS_KEY: rows}, ensure_ascii=False),
            }],
        }

    @staticmethod
    def _text_of(*, message) -> str:
        return ''.join(block.text for block in message.content if block.type == 'text')

    def run(self) -> int:
        """Answer every pending chunk in turn, saving after each one."""
        requests, answers = self.pending()
        if not requests:
            print("nothing to answer; every task row already has an answer")
            return 0
        print(f"{len(requests)} chunks to answer with {self._model}")
        filled = 0
        for position, request in enumerate(requests, start=1):
            expected = {identify(row=row) for row in request.rows}
            rows = self._ask(request=request, expected=expected)
            if rows is None:
                continue
            answer = answers[request.task_number]
            filled += answer.add(rows=rows)
            answer.save()
            print(f"[{position}/{len(requests)}] task {request.task_number:02d}: "
                  f"{len(rows)} of {len(request.rows)} rows answered")
        return filled

    def _ask(self, *, request: Request, expected: typing.AbstractSet[str]) -> typing.Optional[typing.List[dict]]:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                message = self._client.messages.create(**self._parameters(rows=request.rows))
                return AnswerParser.parse(text=self._text_of(message=message), expected=expected)
            except anthropic.AuthenticationError:
                # Every remaining chunk would fail the same way, and each would sleep first.
                raise
            except (MalformedAnswer, anthropic.APIError) as error:
                print(f"attempt {attempt} on {request.custom_id} failed: {error}", file=sys.stderr)
                if attempt < MAX_ATTEMPTS:
                    time.sleep(RETRY_PAUSE_SECONDS)
        print(f"giving up on {request.custom_id}; re-run to try it again", file=sys.stderr)
        return None

    def run_batched(self) -> int:
        """Submit every pending chunk as one batch, then write the answers it returns.

        Half the price of asking one at a time, at the cost of waiting: the batch is answered
        when the service gets to it, not when this process asks.
        """
        requests, answers = self.pending()
        if not requests:
            print("nothing to answer; every task row already has an answer")
            return 0
        by_id = {request.custom_id: request for request in requests}
        try:
            batch = self._client.messages.batches.create(requests=[
                {'custom_id': request.custom_id, 'params': self._parameters(rows=request.rows)}
                for request in requests
            ])
        except anthropic.APIError as error:
            print(f"submitting the batch failed: {error}", file=sys.stderr)
            return 0
        print(f"submitted batch {batch.id} with {len(requests)} chunks; waiting")
        while batch.processing_status != BATCH_ENDED_STATUS:
            time.sleep(BATCH_POLL_SECONDS)
            batch = self._client.messages.batches.retrieve(message_batch_id=batch.id)
        filled = 0
        for result in self._client.messages.batches.results(message_batch_id=batch.id):
            request = by_id.get(result.custom_id)
            if request is None:
                continue
            if result.result.type != 'succeeded':
                print(f"{result.custom_id}: {result.result.type}", file=sys.stderr)
                continue
            expected = {identify(row=row) for row in request.rows}
            try:
                rows = AnswerParser.parse(text=self._text_of(message=result.result.message), expected=expected)
            except MalformedAnswer as error:
                print(f"{result.custom_id}: {error}", file=sys.stderr)
                continue
            answer = answers[request.task_number]
            filled += answer.add(rows=rows)
            answer.save()
        return filled


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=pathlib.Path, help="Directory holding INSTRUCTIONS.md and task files")
    parser.add_argument('--model', default=DEFAULT_MODEL, help="Model id to answer with")
    parser.add_argument('--chunk', type=int, default=DEFAULT_CHUNK_SIZE, help="Rows per request")
    parser.add_argument(
        '--batch',
        action='store_true',
        help="Submit everything as one batch: half price, answered whenever the service gets to it",
    )
    arguments = parser.parse_args()

    runner = TaskRunner(directory=arguments.directory, model=arguments.model, chunk_size=arguments.chunk)
    filled = runner.run_batched() if arguments.batch else runner.run()
    print(f"answered {filled} rows into {arguments.directory}")


if __name__ == '__main__':
    main()
