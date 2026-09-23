# Vocabulary → Anki

Turns a list of words you are learning into finished Anki cards: definition in the target
language, translations, example sentences with translations, synonyms, a picture and a
pronunciation. Built for Spanish first, but nothing in it is Spanish-specific — you name the
language you are learning and your own, and the rest follows.

## Adding words

Write them into `words/<language>.txt`, one per line, then build:

```
python build.py --target es
```

Some languages have an official word list published by an exam board, and where one exists it is
a better starting point than anything you would type by hand — it brings the level, the grammar
and an example sentence with every word. German has one, imported in a single command before the
first build:

```
python -m pipeline.goethe --target de
```

The words it writes and the words you add by hand live in the same table, so both commands can be
re-run at any time.

A word already in the deck is ignored, so old lines can stay in the file. Nothing is thrown
away and nothing already paid for is recomputed — running the build after adding five words
does five words of work.

The build stops and names what it is waiting for. Two stages need a model and cannot run on
their own; where they have work outstanding, the build writes it out as task files and says so.
Answer those, run the build again, and it picks up from there.

## Where things live

| | |
|---|---|
| `words/` | the lists you keep by hand — the only files you edit |
| `languages/` | one small config per language: which dictionaries and media sources to use |
| `templates/` | how a card looks: two card types, plus the stylesheet |
| `sources/` | raw imports, such as a vocabulary app's backup |
| `pipeline/` | the stages |
| `data/` | everything generated, one directory per language |
| `.agents/` | what the pipeline does and why, in prose |

Nothing under `data/` is written by hand. It can be deleted and rebuilt, though the caches in
it are what make a rebuild cheap, so deleting them means paying for every lookup again.

## What a card holds

Two cards per word: recognising the foreign word, and producing it from your own language.
Both carry the headword with its article and gender, a definition written at roughly A2 level,
three example sentences each translated into your language and into English, synonyms and
antonyms, a picture where the word names something you could photograph, and a recording.
Level, topic, part of speech and whether the word is a physical object become tags.

## Running one stage on its own

The build runs them in order, but each is a module you can run alone while working on it:

```
python -m pipeline.dictionaries --target es
python -m pipeline.media --target es --only audio
python -m pipeline.preview --target es --word boca
python -m pipeline.anki --target es
```

`pipeline.preview` renders real cards from real rows into an HTML file, so the design can be
judged without opening Anki.

## Anki

The deck is pushed over the AnkiConnect add-on, which exposes a local API while Anki is
running — so Anki has to be open. The push is idempotent: notes are matched on their key, then
updated or created, never duplicated.

Pictures and recordings already on a note are left alone, because they also arrive from places
this pipeline does not control. Passing `--replace-media` makes the manifest win instead, which
is how a picture judged wrong actually leaves the deck.
