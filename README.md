# Vocabulary → Anki

Turns a list of words you are learning into finished Anki cards: a definition in the language you
are learning, translations, example sentences, synonyms and antonyms, a verb's forms, a picture
and a recording.

The point is that almost nothing on a card is invented. A published dictionary decides what a
word means, a published graded word list decides how hard it is, and a frequency table decides
which words you meet first. A model is asked only for what nobody publishes: restating a dense
definition in simple words, and translating.

Three decks run on it today — English, Spanish, German. The first one built was Spanish, and the
code shows it in a few places; [Language support](#language-support) says exactly where.

## What you need

- Python 3.11 or newer, and `pip install -r requirements.txt`.
- Anki with the [AnkiConnect](https://ankiweb.net/shared/info/2055492159) add-on. It opens a local
  API while Anki is running, which is how the deck is pushed. Anki has to be open for that step.
- `ANTHROPIC_API_KEY`, only if you want the model-backed stages answered for you. You can answer
  them by hand instead, and then nothing is needed.
- `PIXABAY_API_KEY`, optional. It adds one image source; without it that source turns itself off.

## Building a deck

Write the words into `words/<language>.txt`, one per line, then run:

```
python build.py --target en --native ru
```

`--target` is the language you are learning, `--native` your own. Both are two-letter codes.
`--native` defaults to Russian, which is the only thing in the defaults that assumes anything
about you.

The build runs the stages in order and each one skips what is already done, so a run after adding
five words does five words of work. Nothing is thrown away and nothing already paid for is
recomputed. A word already in the deck is ignored, so old lines can stay in the file.

Useful flags: `--skip-media` leaves pictures and recordings alone, `--no-push` stops before Anki.

### The build will stop and wait for you

Three stages need judgement a script cannot supply — which dictionary sense a card should teach,
the fields no dictionary publishes, and the missing example translations. Where those have work
outstanding, the stage writes it out as numbered task files with a `INSTRUCTIONS.md` beside them
and the build says so. Answer them, run the build again, and it carries on.

You can answer them three ways:

```
python -m pipeline.llm data/en/llm --model claude-sonnet-5   # through the API
./enrich.sh                                                  # the same, for a crontab
```

or by writing the `answer-NN.json` files yourself, which is what an agent session does. `enrich.sh`
takes a lock, logs, and never touches Anki — publishing a deck stays a person's decision.

One warning that has bitten here: **clear a stage's task directory before planning a new round.**
The merge step reads every answer file it finds, so answers to a question no longer being asked
get applied again, and the correction you were making is silently reverted.

### Starting from a published word list

Some languages have an official list published by an exam board, and it beats anything you would
type by hand: it brings the level, the grammar and an example sentence with every word. German has
one, imported once before the first build:

```
python -m pipeline.goethe --target de
```

For other languages a graded lexicon fills the level where it can:

```
python -m pipeline.graded_lexicon --target en
```

Words from these and words you add by hand live in the same table, so both can be re-run any time.

### Teaching the book you are reading

The words in a book you are actually reading are better than any list, and a phrase the author
wrote beats an invented example:

```
python -m pipeline.book_import tasks --target en --epub sources/some-book.epub
python -m pipeline.book_import rows  --target en --subdeck 'Some Book'
```

It keeps only the words the frequency table says you do not know yet, each on a phrase from the
book. This one is English-only for now — see [Language support](#language-support).

## What a card holds

A word gets up to three cards, and which ones is set per language:

| | asks |
|---|---|
| recognition | the bare word — what does this mean? |
| context | a sentence with the word marked — what does the marked word mean? |
| recall | your own language — what is the word? |

Where a word has a sentence worth using, the context card replaces the recognition one rather than
joining it: both ask what a word means, and the one with more to go on is the one worth answering.
A word with no such sentence keeps the bare card. The English deck drops `recall` entirely, because
that learner already reads English and never needs to produce these words.

Every card carries the headword with its article and gender, a definition at roughly A2 level
with translations, three example sentences each translated, synonyms and antonyms with glosses,
the headword's other meanings, a verb's forms, a picture where the word names something you could
photograph, and a recording. Level, frequency band, part of speech and topic become tags.

Cards go into a subdeck named for their CEFR level. Anki hands out new cards subdeck by subdeck in
name order, and level names already sort into study order, so that buys the whole progression for
free. Inside one level the order is by frequency, because a level holds well over a year of study
and the order inside it decides most of what you actually see. Levels you have finished can be
named in the config; their cards are suspended rather than deleted and move to a deck of their own.

To see a card without opening Anki:

```
python -m pipeline.preview --target en --word resilience
```

## Pushing to Anki

```
python -m pipeline.anki --target en
```

Idempotent: a note is looked up by its key first, then updated or created. Anki silently refuses a
note whose first field already exists, and refusal looks exactly like success from the outside,
which is why the lookup is not optional. The two counts are reported separately.

Pictures and recordings already on a note are left alone, because they also arrive from places
this pipeline does not control — a text-to-speech pass inside Anki, a hand-picked replacement.
`--replace-media` makes the manifest win instead, which is how a picture judged wrong actually
leaves the deck. `--design-only` pushes edited templates and styling without touching notes.

## Where things live

| | |
|---|---|
| `words/` | the lists you keep by hand — the only files you edit |
| `languages/` | one small config per language: dictionaries, media sources, card types, decks |
| `templates/` | how a card looks; `templates/<code>/` overrides one deck's design |
| `sources/` | raw imports, such as a vocabulary app's backup or an ebook |
| `pipeline/` | the stages |
| `data/` | everything generated, one directory per language |
| `.agents/` | what the pipeline does and why, in prose |

Nothing under `data/` is written by hand. It can be deleted and rebuilt, though the caches in it
are what make a rebuild cheap, so deleting them means paying for every lookup again. It is not in
the repository, and neither are the raw imports.

## Running one stage on its own

Every stage is a module you can run alone while working on it. `--target` is always required.

| | |
|---|---|
| `pipeline.wordlist` | read the hand-kept list into the table |
| `pipeline.dictionaries` | definitions, synonyms, antonyms, corpus sentences |
| `pipeline.sense_choice` | which sense of a new word its card teaches |
| `pipeline.model` | the fields no dictionary publishes, and simpler wording |
| `pipeline.translations` | fill the missing example translations |
| `pipeline.relations` | sort the synonyms and gloss them |
| `pipeline.senses` | the headword's other meanings |
| `pipeline.inflections` | a verb's forms from the Wiktionary conjugation table |
| `pipeline.transcription` | the phonetic transcription |
| `pipeline.frequency` | the frequency band |
| `pipeline.graded_lexicon` | the level, from a published graded list |
| `pipeline.context_cards` | pick the sentence a context card is built on |
| `pipeline.media` | recordings and pictures from curated sources |
| `pipeline.images` | pictures that need looking at before they land |
| `pipeline.anki` | push the notes |
| `pipeline.ordering` | order the new-card queue by frequency |
| `pipeline.mastered` | suspend the levels you have finished |
| `pipeline.preview` | render cards to a local HTML file |

Repair stages, for problems found after the fact: `pipeline.duplicates` folds together cards that
teach one word twice, `pipeline.sense_pruning` takes off the related words belonging to another
meaning, `pipeline.primary_sense` asks a reviewer whether a card teaches the sense a learner meets
first.

Most of these take `--dry-run`, and the ones that need judgement take `--plan` to write task files
and `--from-json` to merge the answers.

### A searched picture is looked at before it lands

`pipeline.media` runs a chain of providers declared per language, and it carries only sources
curated before they arrived — whatever the word list shipped, a visual dictionary where a person
chose the photograph. Anything found by searching goes through `pipeline.images`, which offers
candidates and takes a verdict.

That split is deliberate and not a convenience. The moment two paths can write the same picture
cell, the cheap one silently undoes the careful one: a media run here re-pictured the very words a
review had twice cleared as unpicturable. A language with no curated image source simply has an
empty chain, and every picture comes through review.

## Language support

The design rule is that no stage names a language. Roles — the language being learned, your own,
and English as a pivot for image search — are substituted from `pipeline/language_config.py` and
from `languages/<code>.json`, and the table's column names are rendered from the same place. Most
of the pipeline holds to that. Some of it does not, and the honest list is:

**Only ever going to work for one language, by design.** The Goethe-Institut importer reads the
German exam board's PDFs. Nothing depends on it, so another language just does not run it.

**Assumes your own language is Russian.** The sentence corpus lookup filters translations by fixed
language codes, so a corpus that answers in the right language is still thrown away — German came
back glossed in French and Dutch and every sentence was dropped, which reads as "the corpus has
nothing". The other-meanings merge reads its answers under a fixed Russian key. The media file
names transliterate Cyrillic and Spanish accents only.

**Assumes the language you are learning is English.** The book importer, throughout: English
suffix rules, an English-only alphabet, English function words, the `to` infinitive marker, fixed
Russian and English column names, and English-only frequency lookups. Card-folding and context-card
choice both carry English suffix rules and English function words. Sense pruning carries English
filler words.

**Assumes the language you are learning is Spanish.** The reviewer-facing sense check writes
Spanish into its prompt instead of substituting it. The visual dictionary provider matches a
Spanish marker in the page.

The pattern behind the list: the original pipeline was parameterised from the start, and the
stages added later while reworking one deck were written straight against that deck. None of them
is hard to parameterise — the config already carries everything they need — but until that is
done, running them against another language produces wrong cards rather than an error.

## Licence and sources

The code here is mine to publish; what it fetches is not. Every dictionary, corpus, graded list
and image archive it queries has its own terms, and the media manifest records the licence and
attribution for each picture because that cannot be recovered once the file is downloaded. One
source — the Spanish visual dictionary — states no licence at all, so it is for personal use only.
Check the terms before you publish a deck built with this.
