"""Choose each card's picture by looking at candidates, not by trusting a search ranking.

An image search ranks on text relevance, and the top hit for an everyday noun is routinely a
picture of something else — an X-ray of a skull for "head", a stock logo for "market". A wrong
picture costs more than no picture: it sits next to the definition and teaches against it. So
this stage collects several candidates per word and hands them to something that can actually
look at them, which picks one or rejects them all.

The search term matters more than the ranking. A bare dictionary translation is a poor query
because image archives are captioned in idiom, not in dictionary senses: "arm" retrieves coats
of arms and armed forces, "seal" retrieves the animal. So the query is written for each word
by something that knows which sense the card teaches, and only then searched.

Three steps: `--plan-queries` asks for those queries, `--plan` downloads candidates with them,
`--from-json` installs the choices. Only words that name something photographable are
considered, and only where the current picture came from a search — whatever the source word
list supplied was already matched to the right sense by a human and is left alone.
"""

import argparse
import concurrent.futures
import json
import os
import pathlib
import shutil
import typing
import urllib.parse

from . import reword_media
from . import media
from . import language_config
from . import tasks

CARDS_FILENAME = 'cards.tsv'
MANIFEST_FILENAME = 'media.tsv'
MEDIA_DIRECTORY = 'media'
CANDIDATE_DIRECTORY = 'candidates'
TASK_DIRECTORY = 'image_review'
QUERY_DIRECTORY = 'image_queries'
AUDIT_DIRECTORY = 'image_audit'
INSTRUCTIONS_FILE = 'INSTRUCTIONS.md'
CANDIDATE_MANIFEST = 'candidates.json'
ANSWER_SUFFIX = '.json'

SLICE_COUNT = 8
CANDIDATES_PER_WORD = 6
SEARCH_RESULT_COUNT = 20
# Photographs only: an illustration or a diagram of a common object reads as clip art on a card.
SEARCH_URL_TEMPLATE = (
    f'https://{media.OPENVERSE_HOST}/v1/images/'
    '?q={query}&license_type=commercial&category=photograph&page_size={page_size}{sources}'
)
# Stock-photo libraries caption a plain subject plainly, so they answer a vocabulary query far
# better than the general index — which is mostly news, museum and personal photography, where
# an everyday noun mostly appears in captions about something else. Their coverage is narrow,
# so the general index still runs afterwards for the words they do not carry.
STOCK_SOURCES = ('stocksnap', 'rawpixel', 'nappy')

PIXABAY_API_KEY = os.getenv('PIXABAY_API_KEY', '')
PIXABAY_HOST = 'pixabay.com'
PIXABAY_SOURCE_NAME = 'pixabay'
PIXABAY_LICENSE = 'pixabay content license'
PIXABAY_SEARCH_URL_TEMPLATE = (
    f'https://{PIXABAY_HOST}/api/?key={{key}}&q={{query}}&image_type=photo&safesearch=true'
    '&order=popular&per_page={page_size}'
)
# Providers that guessed; anything else was matched to the sense by a human and stays.
SEARCHED_SOURCES = ('openverse', 'wikipedia')

AUDIT_INSTRUCTIONS_TEMPLATE = """# Checking the picture a card already carries

Each task file lists words with the picture currently on the card. Look at every one with the Read
tool and say whether it should stay.

## Output

Write `answer-NN.json` next to the task file, `NN` matching the task file's number:

```json
{{"rows": [{{"key": "<copied verbatim>", "keep": false, "why": "<a few words>"}}]}}
```

One row per word in your task file, no omissions. `keep` is a real boolean. `why` is one short
phrase, in English, and is only needed when you reject.

## What to keep

Keep a picture when someone who does not know the word could look at it and arrive at the word's
`meaning` — the sense this card teaches, not some other sense of the same spelling. The subject
should be the obvious thing in the frame, not something you have to hunt for or be told about.

Reject when the picture shows a neighbouring thing rather than the word: a whole face where the
word is `mouth`, a person where the word is their profession, a landscape where the word is one
building in it. This is the common failure and the hardest to see, because such a picture always
looks reasonable until you ask what a learner would actually name.

Reject too: X-rays, cutaways, diagrams and clip art standing in for a plain object; text as the
subject; a picture whose real subject is a different thing that merely contains the word.

Be strict but not fussy. A plain, slightly dull photograph of the right thing is a keep. Style,
age and image quality are not grounds for rejection — only what the picture shows.


## Verifying

Before finishing, check with Python that the file parses, every `key` matches your task file
verbatim, every row is answered, and `keep` is a boolean everywhere. Report how many you kept and
how many you rejected.
""" + tasks.INCREMENTAL_SAVING

QUERY_INSTRUCTIONS_TEMPLATE = """# Writing photo-search queries

Each task file lists words with the sense their card teaches. Write the English query that
would retrieve a plain photograph of that thing from a stock-photo archive.

## Output

Write `answer-NN.json` next to the task file, `NN` matching the task file's number:

```json
{{"rows": [{{"key": "<copied verbatim>", "query": "human forearm"}}]}}
```

One row per word in your task file, no omissions.

## What makes a good query

Two or three English words, lowercase, no punctuation, no quotes. The query names the thing
itself, as a photographer would caption it.

A one-word query is usually wrong. Archive captions are written in idiom, so a bare dictionary
word drags in every other use of that spelling: `arm` retrieves coats of arms and armed forces,
`seal` retrieves the animal, `head` retrieves heads of state and X-rays. Add the word that
pins the sense down — `human arm`, `rubber stamp`, `person's head`.

`meaning` and `definition` give the sense the card teaches, and they win over the word's more
famous homonym. For a word naming a person by their role, ask for the person at work —
`waiter serving table`, not `restaurant`.

Do not add photographic jargon (`isolated`, `white background`, `stock photo`, `4k`) — the
archive is not a stock library and those terms only narrow it to nothing.


## Verifying

Before finishing, check with Python that the file parses, every `key` matches your task file
verbatim, every row is answered, and no query is empty or longer than four words. Report those
counts.
""" + tasks.INCREMENTAL_SAVING

INSTRUCTIONS_TEMPLATE = """# Choosing card pictures

Each task file lists words with candidate photographs. Look at every candidate with the Read
tool, then choose the one that best pictures the word — or reject them all.

## Output

Write `answer-NN.json` next to the task file, `NN` matching the task file's number:

```json
{{"rows": [{{"key": "<copied verbatim>", "choice": 2, "why": "<a few words>"}}]}}
```

`choice` is the candidate's `number`, or `0` to take none of them. One row per word in your
task file, no omissions. `why` is one short phrase, in English, for the record.

## What makes a good candidate

The picture has to be recognisable as the word **on its own**, at the size of a phone screen,
by someone who does not yet know the word. That means one clear subject, filling most of the
frame, photographed plainly.

Reject anything that needs a caption to make sense: an X-ray or cutaway where a plain photo is
what the word means, a diagram, a screenshot, a logo, a book cover, a meme, a picture whose
real subject is something else with the word merely present somewhere in it.

`meaning` gives the sense this card teaches — a word with several senses gets a picture of that
one sense, not of its more famous homonym. When the word names a person by role, the picture
should show a person doing that role, not an object associated with it.

Prefer no picture over a doubtful one: an abstract or ambiguous word is better bare, and `0` is
a perfectly good answer. Do not stretch to fill every row.


## Verifying

Before finishing, check with Python that the file parses, every `key` matches your task file,
every `choice` is either `0` or a `number` that exists for that key, and that you answered every
row. Report those counts.
""" + tasks.INCREMENTAL_SAVING


class CandidateFetcher:
    """Downloads the search results a word will be judged on."""

    def __init__(self, *, language: language_config.LanguageConfig, data_directory: pathlib.Path):
        self._language = language
        self._directory = data_directory / CANDIDATE_DIRECTORY
        self._cache = media.Cache(root=data_directory / media.CACHE_DIRECTORY_NAME)
        self._stats = media.Stats()
        self._throttles = media.HostThrottleRegistry(
            max_workers_per_host=media.MAX_WORKERS_PER_HOST,
            min_interval_seconds=media.HOST_MIN_REQUEST_INTERVAL_SECONDS,
        )

    @property
    def stats(self) -> media.Stats:
        return self._stats

    def collect(self, *, row: dict, slug: str, query: str = '') -> typing.Optional[dict]:
        query = query or media.RowFields.search_term(row=row, language=self._language)
        if not query:
            return None
        results = self._search(query=query)
        if not results:
            return None
        directory = self._directory / slug
        directory.mkdir(parents=True, exist_ok=True)
        candidates = []
        for result in self._rank(results=results, query=query):
            if len(candidates) >= CANDIDATES_PER_WORD:
                break
            preview = result.get('thumbnail') or result.get('url')
            if not preview:
                continue
            downloaded = media.MediaDownloader.fetch_and_validate_image(
                url=preview, throttles=self._throttles, stats=self._stats,
            )
            if downloaded is None:
                continue
            content, extension = downloaded
            number = len(candidates) + 1
            path = directory / f'{number}.{extension}'
            path.write_bytes(content)
            candidates.append({
                'number': number,
                'preview': str(path),
                'title': (result.get('title') or '').strip(),
                'url': result.get('url') or '',
                'license': result.get('license') or '',
                'attribution': media.OpenverseImageProvider.attribution(result=result),
            })
        if not candidates:
            return None
        entry = {'key': row['key'], 'slug': slug, 'query': query, 'candidates': candidates}
        (directory / CANDIDATE_MANIFEST).write_text(
            json.dumps(entry, ensure_ascii=False, indent=1), encoding='utf-8',
        )
        return entry

    def _search(self, *, query: str) -> typing.List[dict]:
        """Stock libraries first, then the general index for whatever they did not cover."""
        merged: typing.Dict[str, dict] = {}
        for result in self._search_pixabay(query=query):
            merged.setdefault(result['id'], result)
        for sources in (STOCK_SOURCES, ()):
            for result in self._search_once(query=query, sources=sources):
                merged.setdefault(result.get('id') or result.get('url', ''), result)
        return list(merged.values())

    def _search_pixabay(self, *, query: str) -> typing.List[dict]:
        """A dedicated stock library, when its key is configured; silently skipped otherwise."""
        if not PIXABAY_API_KEY:
            return []
        identifier = f'pixabay:{query}'
        cached = self._cache.load(provider=PIXABAY_SOURCE_NAME, identifier=identifier)
        if cached is None:
            url = PIXABAY_SEARCH_URL_TEMPLATE.format(
                key=PIXABAY_API_KEY, query=urllib.parse.quote(query), page_size=SEARCH_RESULT_COUNT,
            )
            response = media.HttpClient.get_text(
                url=url, throttle=self._throttles.for_host(host=PIXABAY_HOST), stats=self._stats,
            )
            if response.status == media.FetchStatus.FAILED or not response.text:
                return []
            hits = json.loads(response.text).get('hits') or []
            cached = {'found': bool(hits), 'results': [self._as_result(hit=hit) for hit in hits]}
            self._cache.store(provider=PIXABAY_SOURCE_NAME, identifier=identifier, payload=cached)
        return cached.get('results', [])

    @staticmethod
    def _as_result(*, hit: dict) -> dict:
        """Reshapes a Pixabay hit into the shape the rest of this stage already speaks."""
        return {
            'id': f"pixabay-{hit.get('id')}",
            'source': PIXABAY_SOURCE_NAME,
            'title': hit.get('tags', ''),
            'url': hit.get('largeImageURL') or hit.get('webformatURL', ''),
            'thumbnail': hit.get('webformatURL', ''),
            'license': PIXABAY_LICENSE,
            'creator': hit.get('user', ''),
            'foreign_landing_url': hit.get('pageURL', ''),
        }

    def _search_once(self, *, query: str, sources: typing.Sequence[str]) -> typing.List[dict]:
        identifier = f"photograph:{','.join(sources) or 'any'}:{query}"
        cached = self._cache.load(provider=media.ImageProviderName.OPENVERSE, identifier=identifier)
        if cached is not None:
            return cached.get('results', [])
        url = SEARCH_URL_TEMPLATE.format(
            query=urllib.parse.quote(query),
            page_size=SEARCH_RESULT_COUNT,
            sources=f"&source={','.join(sources)}" if sources else '',
        )
        response = media.HttpClient.get_text(
            url=url, throttle=self._throttles.for_host(host=media.OPENVERSE_HOST), stats=self._stats,
        )
        if response.status == media.FetchStatus.FAILED or not response.text:
            return []
        results = json.loads(response.text).get('results') or []
        self._cache.store(
            provider=media.ImageProviderName.OPENVERSE,
            identifier=identifier,
            payload={'found': bool(results), 'results': results},
        )
        return results

    @staticmethod
    def _rank(*, results: typing.List[dict], query: str) -> typing.List[dict]:
        """A dedicated stock library first, then the rest, each group by title relevance."""
        term = query.lower()
        return sorted(
            results,
            key=lambda result: (
                result.get('source') != PIXABAY_SOURCE_NAME,
                result.get('source') not in STOCK_SOURCES,
                term not in (result.get('title') or '').lower(),
            ),
        )


class ReviewTasks:
    """Task and answer files on disk, one slice per reviewer."""

    @staticmethod
    def describe(*, row: dict, language: language_config.LanguageConfig) -> dict:
        """What a reviewer needs to tell one sense of a headword from another."""
        return {
            'key': row['key'],
            'word': row.get('word', ''),
            'meaning': row.get(language.translations_native_column, ''),
            'definition': row.get(language.definition_column, ''),
        }

    @classmethod
    def for_queries(cls, *, rows: typing.List[dict], language: language_config.LanguageConfig) -> typing.List[dict]:
        return [cls.describe(row=row, language=language) for row in rows]

    @classmethod
    def for_audit(cls, *, rows: typing.List[dict], manifest, media_directory: pathlib.Path,
                  language: language_config.LanguageConfig) -> typing.List[dict]:
        """Every word that already has a picture, so the picture itself can be looked at."""
        tasks = []
        for row in rows:
            filename = manifest.get(key=row['key'], column='image')
            if not filename or not (media_directory / filename).exists():
                continue
            tasks.append({
                **cls.describe(row=row, language=language),
                'picture': str(media_directory / filename),
                'source': manifest.get(key=row['key'], column='image_source'),
            })
        return tasks

    @classmethod
    def for_review(cls, *, entries: typing.List[dict], rows: typing.Dict[str, dict],
                   language: language_config.LanguageConfig) -> typing.List[dict]:
        return [
            {
                **cls.describe(row=rows[entry['key']], language=language),
                'candidates': [
                    {'number': candidate['number'], 'preview': candidate['preview'], 'title': candidate['title']}
                    for candidate in entry['candidates']
                ],
            }
            for entry in entries
        ]

    @staticmethod
    def write(*, tasks: typing.List[dict], directory: pathlib.Path, slices: int,
              instructions: str) -> typing.List[pathlib.Path]:
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
        (directory / INSTRUCTIONS_FILE).write_text(instructions, encoding='utf-8')
        return written

    @staticmethod
    def load_answers(*, directory: pathlib.Path) -> typing.Dict[str, dict]:
        answers: typing.Dict[str, dict] = {}
        for path in sorted(directory.glob(f'*{ANSWER_SUFFIX}')):
            if path.name.startswith('task-'):
                continue
            for row in json.loads(path.read_text(encoding='utf-8')).get('rows', []):
                answers[row['key']] = row
        return answers


class ChoiceInstaller:
    """Puts the chosen candidate where the card expects it, and clears a rejected one."""

    def __init__(self, *, data_directory: pathlib.Path):
        self._media_directory = data_directory / MEDIA_DIRECTORY
        self._candidate_directory = data_directory / CANDIDATE_DIRECTORY

    def install(self, *, entry: dict, choice: int, manifest) -> bool:
        key = entry['key']
        if choice == 0:
            self.clear(key=key, manifest=manifest)
            return False
        candidate = next((item for item in entry['candidates'] if item['number'] == choice), None)
        if candidate is None:
            return False
        source = pathlib.Path(candidate['preview'])
        target = self._media_directory / f"{entry['slug']}{source.suffix}"
        shutil.copyfile(source, target)
        self._replace(key=key, filename=target.name, manifest=manifest)
        manifest.set(key=key, column='image_source', value=media.ImageProviderName.OPENVERSE)
        manifest.set(key=key, column='image_license', value=candidate['license'])
        manifest.set(key=key, column='image_attribution', value=candidate['attribution'])
        return True

    def clear(self, *, key: str, manifest) -> None:
        self._replace(key=key, filename='', manifest=manifest)
        for column in ('image_source', 'image_license', 'image_attribution'):
            manifest.set(key=key, column=column, value='')

    def _replace(self, *, key: str, filename: str, manifest) -> None:
        """Drop the file the manifest used to name, so rejected pictures leave nothing behind."""
        previous = manifest.get(key=key, column='image')
        if previous and previous != filename:
            (self._media_directory / previous).unlink(missing_ok=True)
        manifest.set(key=key, column='image', value=filename)


def reviewable(*, rows: typing.List[dict], manifest, retry_rejected: bool = False) -> typing.List[dict]:
    """Photographable words still waiting for a picture a search could supply.

    Searching splits cleanly from auditing: this fills empty cells, the audit empties cells
    holding a picture that turned out wrong. A word already carrying a chosen picture is
    settled, and re-searching it would put a reviewed decision back up for grabs.

    A rejection leaves the cell as empty as a word that was never searched, so without a
    memory of it a rejected word would be searched again on every run, forever. Pass
    `retry_rejected` to ask again anyway — worth doing when a new source appears.
    """
    selected = []
    for row in rows:
        if not media.RowFields.is_object(row=row):
            continue
        if not retry_rejected and row.get('image_rejected'):
            continue
        source = manifest.get(key=row['key'], column='image_source')
        if source and source not in SEARCHED_SOURCES:
            continue
        if manifest.get(key=row['key'], column='image'):
            continue
        selected.append(row)
    return selected


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--audit', action='store_true', help="Write task files asking whether each current picture fits")
    parser.add_argument('--audit-from', type=pathlib.Path, default=None, help="Directory holding audit answers")
    parser.add_argument('--plan-queries', action='store_true', help="Write task files asking for search queries")
    parser.add_argument('--plan', action='store_true', help="Download candidates and write task files")
    parser.add_argument('--queries-from', type=pathlib.Path, default=None, help="Directory holding answered queries")
    parser.add_argument('--from-json', type=pathlib.Path, default=None, help="Directory holding answer files")
    parser.add_argument('--slices', type=int, default=SLICE_COUNT)
    parser.add_argument('--limit', type=int, default=None, help="Process only the first N words")
    parser.add_argument(
        '--retry-rejected', action='store_true',
        help="Search again for words a reviewer previously rejected",
    )
    language_config.add_language_arguments(parser)
    arguments = parser.parse_args()

    root = language_config.PROJECT_ROOT
    language = language_config.language_from(arguments, root=root)
    data_directory = language.data_directory(root=root)
    rows = language_config.TsvFile.read(data_directory / CARDS_FILENAME)
    manifest = media.ManifestStore.load(path=data_directory / MANIFEST_FILENAME)
    by_key = {row['key']: row for row in rows}

    media_directory = data_directory / MEDIA_DIRECTORY

    if arguments.audit:
        directory = data_directory / AUDIT_DIRECTORY
        tasks = ReviewTasks.for_audit(
            rows=rows, manifest=manifest, media_directory=media_directory, language=language,
        )
        written = ReviewTasks.write(
            tasks=tasks, directory=directory, slices=arguments.slices,
            instructions=AUDIT_INSTRUCTIONS_TEMPLATE,
        )
        print(f"{len(tasks)} cards carry a picture; wrote {len(written)} task files to {directory}")
        return

    if arguments.audit_from is not None:
        answers = ReviewTasks.load_answers(directory=arguments.audit_from)
        installer = ChoiceInstaller(data_directory=data_directory)
        kept = cleared = 0
        for key, answer in answers.items():
            if answer.get('keep'):
                kept += 1
                continue
            installer.clear(key=key, manifest=manifest)
            cleared += 1
        manifest.write(columns=language_config.MEDIA_MANIFEST_COLUMNS)
        print(f"kept {kept} pictures, removed {cleared}")
        return

    candidates_for = reviewable(rows=rows, manifest=manifest, retry_rejected=arguments.retry_rejected)
    if arguments.limit is not None:
        candidates_for = candidates_for[:arguments.limit]

    if arguments.plan_queries:
        directory = data_directory / QUERY_DIRECTORY
        written = ReviewTasks.write(
            tasks=ReviewTasks.for_queries(rows=candidates_for, language=language),
            directory=directory, slices=arguments.slices, instructions=QUERY_INSTRUCTIONS_TEMPLATE,
        )
        print(f"{len(candidates_for)} words need a picture; wrote {len(written)} task files to {directory}")
        return

    if arguments.plan:
        queries = {}
        if arguments.queries_from is not None:
            queries = {
                key: answer.get('query', '')
                for key, answer in ReviewTasks.load_answers(directory=arguments.queries_from).items()
            }
        fetcher = CandidateFetcher(language=language, data_directory=data_directory)
        entries = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=media.EXECUTOR_MAX_WORKERS) as pool:
            futures = [
                pool.submit(
                    fetcher.collect,
                    row=row,
                    slug=reword_media.Slugger.slug(key=row['key'], language_code=language.target),
                    query=queries.get(row['key'], ''),
                )
                for row in candidates_for
            ]
            for future in concurrent.futures.as_completed(futures):
                entry = future.result()
                if entry is not None:
                    entries.append(entry)
        entries.sort(key=lambda entry: entry['key'])
        written = ReviewTasks.write(
            tasks=ReviewTasks.for_review(entries=entries, rows=by_key, language=language),
            directory=data_directory / TASK_DIRECTORY, slices=arguments.slices,
            instructions=INSTRUCTIONS_TEMPLATE,
        )
        pictures = sum(len(entry['candidates']) for entry in entries)
        print(f"{len(entries)} of {len(candidates_for)} words got candidates — {pictures} pictures")
        print(f"wrote {len(written)} task files to {data_directory / TASK_DIRECTORY}")
        return

    if arguments.from_json is not None:
        answers = ReviewTasks.load_answers(directory=arguments.from_json)
        installer = ChoiceInstaller(data_directory=data_directory)
        installed = rejected = recorded = 0
        for key, answer in answers.items():
            manifest_path = data_directory / CANDIDATE_DIRECTORY
            slug = reword_media.Slugger.slug(key=key, language_code=language.target)
            entry_path = manifest_path / slug / CANDIDATE_MANIFEST
            if not entry_path.exists():
                continue
            entry = json.loads(entry_path.read_text(encoding='utf-8'))
            choice = int(answer.get('choice', 0))
            row = by_key.get(key)
            if choice == 0:
                if row is not None:
                    row['image_rejected'] = ' '.join(str(answer.get('why', '')).split())
                    recorded += 1
            elif row is not None and row.get('image_rejected'):
                # A picture was found this time, so the earlier rejection no longer applies.
                row['image_rejected'] = ''
            if installer.install(entry=entry, choice=choice, manifest=manifest):
                installed += 1
            else:
                rejected += 1
        manifest.write(columns=language_config.MEDIA_MANIFEST_COLUMNS)
        language_config.TsvFile.write(data_directory / CARDS_FILENAME, rows=rows, columns=language.card_columns)
        print(f"installed {installed} pictures, cleared {rejected}, recorded {recorded} rejections")
        return

    parser.error("choose --audit, --audit-from, --plan-queries, --plan or --from-json")


if __name__ == '__main__':
    main()
