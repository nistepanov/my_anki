"""Extract free media embedded in the ReWord backup and hand it to the Anki push step.

The backup carries photos and MP3 pronunciations already matched to the right words, but
the export that produced words.tsv deliberately dropped the backup's own ids (see
reword.py). So the link has to be rebuilt: for every backup word, recompute the
same headword-plus-first-sense key the exporter used and match it against words.tsv rows.
A words.tsv row was often merged from several backup words with the same key, so the
first backup word that actually carries media wins.

Most pictures are JPEG, but a real minority are PNG despite the schema's column name; the
extension written to disk is decided by sniffing the blob's magic bytes, not assumed.

The key has to come out byte-identical to the exporter's, so every part of building it —
which of the backup's translation columns counts as native, and what the language glues in
front of a headword — is taken from the exporter rather than restated here.
"""

import argparse
import pathlib
import re
import sqlite3
import typing
import unicodedata

from . import reword
from . import language_config

# Standard scientific transliteration for the Cyrillic gloss half of a key.
CYRILLIC_TRANSLITERATION = {
    'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'e',
    'ж': 'zh', 'з': 'z', 'и': 'i', 'й': 'y', 'к': 'k', 'л': 'l', 'м': 'm',
    'н': 'n', 'о': 'o', 'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u',
    'ф': 'f', 'х': 'kh', 'ц': 'ts', 'ч': 'ch', 'ш': 'sh', 'щ': 'shch',
    'ъ': '', 'ы': 'y', 'ь': '', 'э': 'e', 'ю': 'yu', 'я': 'ya',
}
# Doubled rather than dropped: Spanish has real accent-only minimal pairs on the headword
# (e.g. que/qué, como/cómo), so folding the accent away entirely would collide them.
LATIN_ACCENT_TRANSLITERATION = {
    'á': 'aa', 'é': 'ee', 'í': 'ii', 'ó': 'oo', 'ú': 'uu', 'ñ': 'nn', 'ü': 'uu',
}
TRANSLITERATION = {**CYRILLIC_TRANSLITERATION, **LATIN_ACCENT_TRANSLITERATION}
SLUG_COLLAPSE_PATTERN = re.compile(r'[^a-z0-9]+')
SLUG_MAX_LENGTH = 80

JPEG_EXTENSION = 'jpg'
PNG_EXTENSION = 'png'
PNG_MAGIC_BYTES = b'\x89PNG\r\n\x1a\n'
AUDIO_EXTENSION = 'mp3'
MEDIA_DIRECTORY_NAME = 'media'
MANIFEST_FILE_NAME = language_config.MEDIA_MANIFEST_FILENAME
MANIFEST_COLUMNS = language_config.MEDIA_MANIFEST_COLUMNS
AUDIO_SOURCE_NAME = 'source_backup'


class BackupWord(typing.NamedTuple):
    """One WORD row, reduced to what media-matching needs."""

    id: int
    picture_id: typing.Optional[int]


class BackupMedia:
    """Read-only view of a ReWord backup: words indexed by their rebuilt key, plus blobs."""

    def __init__(self, *, connection: sqlite3.Connection, language: language_config.LanguageConfig):
        self._connection = connection
        self._language = language
        self._columns = reword.BackupColumns.resolve(connection=connection, language=language)

    def words_by_key(self) -> typing.Dict[str, typing.List[BackupWord]]:
        """Group every backup word by the same key the export would give it."""
        grouped: typing.Dict[str, typing.List[BackupWord]] = {}
        native = self._columns.native_translation
        query = f'SELECT ID, WORD, {native}, PICTURE_ID FROM WORD ORDER BY ID'
        for record in self._connection.execute(query):
            article, bare_word = reword.WordExporter.split_article(
                word=reword.WordExporter.clean(record['WORD']),
                particles=self._language.articles,
            )
            key = reword.WordExporter.build_key(
                article=article,
                word=bare_word,
                translations_native=reword.WordExporter.clean(record[native]),
            )
            grouped.setdefault(key, []).append(BackupWord(id=record['ID'], picture_id=record['PICTURE_ID']))
        return grouped

    def pictures_with_content(self) -> typing.Dict[int, bytes]:
        query = 'SELECT ID, CONTENT FROM PICTURE WHERE CONTENT IS NOT NULL'
        return {record['ID']: record['CONTENT'] for record in self._connection.execute(query)}

    def audio_with_content(self) -> typing.Dict[str, bytes]:
        query = 'SELECT ID, CONTENT FROM AUDIO WHERE CONTENT IS NOT NULL'
        return {record['ID']: record['CONTENT'] for record in self._connection.execute(query)}

    def audio_id_by_word(self, *, stored: typing.Container[str]) -> typing.Dict[int, str]:
        """Lowest ORD wins, among the recordings the backup actually holds bytes for.

        A word links to several pronunciations and the app keeps only some of them, streaming
        the rest on demand — and the ones it streams sit at the head of the order. Ranking
        before checking for the bytes therefore picks an empty row almost every time and
        discards the recordings that are there.
        """
        best: typing.Dict[int, typing.Tuple[int, str]] = {}
        query = 'SELECT WORD_ID, AUDIO_ID, ORD FROM WORD_AUDIO'
        for word_id, audio_id, ord_value in self._connection.execute(query):
            if audio_id not in stored:
                continue
            if word_id not in best or ord_value < best[word_id][0]:
                best[word_id] = (ord_value, audio_id)
        return {word_id: audio_id for word_id, (_, audio_id) in best.items()}


class Slugger:
    """Turns a words.tsv key into a stable, filesystem- and Anki-safe filename stem."""

    @staticmethod
    def transliterate(text: str) -> str:
        translated = ''.join(TRANSLITERATION.get(character, character) for character in text.lower())
        decomposed = unicodedata.normalize('NFKD', translated)
        return decomposed.encode('ascii', 'ignore').decode('ascii')

    @classmethod
    def slug(cls, *, key: str, language_code: str) -> str:
        collapsed = SLUG_COLLAPSE_PATTERN.sub('-', cls.transliterate(key)).strip('-')
        trimmed = collapsed[:SLUG_MAX_LENGTH].strip('-')
        return f'{language_code}-{trimmed}'

    @staticmethod
    def assert_unique(*, slugs_by_key: typing.Dict[str, str]) -> None:
        """Anki's media folder is one flat namespace, so a collision would silently overwrite a file."""
        owner_by_slug: typing.Dict[str, str] = {}
        for key, slug in slugs_by_key.items():
            assert slug not in owner_by_slug, (
                f"slug collision: {key!r} and {owner_by_slug[slug]!r} both slugify to {slug!r}"
            )
            owner_by_slug[slug] = key


class MediaLinker:
    """Matches one words.tsv row to a backup word and extracts whatever media it has."""

    def __init__(self, *, backup: BackupMedia, language_code: str, media_directory: pathlib.Path):
        self._words_by_key = backup.words_by_key()
        self._pictures = backup.pictures_with_content()
        self._audio = backup.audio_with_content()
        self._audio_id_by_word = backup.audio_id_by_word(stored=self._audio)
        self._language_code = language_code
        self._media_directory = media_directory

    def has_backup_word(self, *, key: str) -> bool:
        return key in self._words_by_key

    def link(self, *, key: str) -> typing.Optional[typing.Dict[str, str]]:
        """Returns the manifest row for this key, or None if no backup word matched or none had media."""
        candidates = self._words_by_key.get(key, [])
        image_content, audio_content = None, None
        for candidate in candidates:
            image_content = self._pictures.get(candidate.picture_id) if candidate.picture_id is not None else None
            audio_id = self._audio_id_by_word.get(candidate.id)
            audio_content = self._audio.get(audio_id) if audio_id is not None else None
            if image_content is not None or audio_content is not None:
                break  # first backup word carrying media wins

        if image_content is None and audio_content is None:
            return None

        slug = Slugger.slug(key=key, language_code=self._language_code)
        row = {'key': key, 'image': '', 'audio': ''}
        if image_content is not None:
            extension = self.image_extension(content=image_content)
            row['image'] = self._write(slug=slug, extension=extension, content=image_content)
        if audio_content is not None:
            row['audio'] = self._write(slug=slug, extension=AUDIO_EXTENSION, content=audio_content)
        return row

    @staticmethod
    def image_extension(*, content: bytes) -> str:
        return PNG_EXTENSION if content.startswith(PNG_MAGIC_BYTES) else JPEG_EXTENSION

    def _write(self, *, slug: str, extension: str, content: bytes) -> str:
        filename = f'{slug}.{extension}'
        path = self._media_directory / filename
        if not path.exists() or path.read_bytes() != content:
            path.write_bytes(content)
        return filename


class ManifestMerge:
    """Providers share one manifest, so a later run must top up rather than replace it."""

    @staticmethod
    def apply(*, path: pathlib.Path, rows: typing.List[dict]) -> typing.List[dict]:
        existing = {}
        if path.exists():
            existing = {row['key']: dict(row) for row in language_config.TsvFile.read(path)}
        for row in rows:
            merged = existing.setdefault(row['key'], {'key': row['key']})
            for column in MANIFEST_COLUMNS:
                if column == 'key' or merged.get(column):
                    continue
                value = row.get(column, '')
                if value:
                    merged[column] = value
            # Record where the media came from, but never overwrite another provider's note.
            if merged.get('audio') and not merged.get('audio_source'):
                merged['audio_source'] = AUDIO_SOURCE_NAME
            if merged.get('image') and not merged.get('image_source'):
                merged['image_source'] = AUDIO_SOURCE_NAME
        return list(existing.values())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('backup', type=pathlib.Path, help="Path to the ReWord .backup SQLite file")
    parser.add_argument('--limit', type=int, default=None, help="Process only the first N rows")
    language_config.add_language_arguments(parser)
    arguments = parser.parse_args()

    root = language_config.PROJECT_ROOT
    language = language_config.language_from(arguments, root=root)
    data_directory = language.data_directory(root=root)
    media_directory = data_directory / MEDIA_DIRECTORY_NAME
    media_directory.mkdir(parents=True, exist_ok=True)

    rows = language_config.TsvFile.read(data_directory / 'words.tsv')
    if arguments.limit is not None:
        rows = rows[:arguments.limit]

    slugs_by_key = {row['key']: Slugger.slug(key=row['key'], language_code=language.target) for row in rows}
    Slugger.assert_unique(slugs_by_key=slugs_by_key)

    connection = sqlite3.connect(f'file:{arguments.backup}?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    linker = MediaLinker(
        backup=BackupMedia(connection=connection, language=language),
        language_code=language.target,
        media_directory=media_directory,
    )

    manifest_rows: typing.List[dict] = []
    images_written, audio_written, unmatched_keys = 0, 0, 0
    for row in rows:
        manifest_row = linker.link(key=row['key'])
        if manifest_row is None:
            if not linker.has_backup_word(key=row['key']):
                unmatched_keys += 1
            continue
        manifest_rows.append(manifest_row)
        images_written += 1 if manifest_row['image'] else 0
        audio_written += 1 if manifest_row['audio'] else 0

    manifest_path = data_directory / MANIFEST_FILE_NAME
    merged = ManifestMerge.apply(path=manifest_path, rows=manifest_rows)
    language_config.TsvFile.write(manifest_path, rows=merged, columns=MANIFEST_COLUMNS)

    keys_without_media = len(rows) - len(manifest_rows) - unmatched_keys
    print(f"processed {len(rows)} rows")
    print(f"images written {images_written} | audio written {audio_written}")
    print(f"keys with no media {keys_without_media} | keys matching no backup word {unmatched_keys}")
    # The manifest is topped up rather than replaced, so reporting this run's haul as its
    # size reads as though the rest had been dropped.
    print(f"manifest holds {len(merged)} rows, {len(manifest_rows)} of them from this run, at {manifest_path}")


if __name__ == '__main__':
    main()
