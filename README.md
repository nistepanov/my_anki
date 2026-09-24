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
python -m pipeline.book_import tasks --target en --directory data/en/book_80_days \
    --epub sources/around-the-world-in-80-days.epub
# answer each task_NN.json into an answer_NN.json beside it, then:
python -m pipeline.book_import rows  --target en --directory data/en/book_80_days \
    --subdeck 'Around the World in 80 Days'
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

Every stage is a module you can run alone while working on it. All of them take `--target` — it is
required and has no default — and `--native`, which defaults to `ru`. Many take `--limit N`, to work
on the first few rows while you are still checking what a stage does. The stages that rewrite rows
without asking anybody first take `--dry-run`, which prints the change and writes nothing.

The commands below use `--target en`; swap the code for another deck.

### The table the stages pass along

Three files under `data/<target>/`, each stage reading one and writing the next or itself:

| | written by | holds |
|---|---|---|
| `words.tsv` | whatever brought the words in | one row per word, most cells empty |
| `enriched.tsv` | the dictionary stage | the same rows with what a dictionary could answer |
| `cards.tsv` | the model stage, then everything after it | the finished rows the Anki push reads |

A stage never drops a column it does not know about, so running them out of order costs a rerun
rather than data.

### Getting words in

```
# the list you keep by hand — this is what build.py calls
python -m pipeline.wordlist words/en.txt --target en

# an exam board's published list, German only
python -m pipeline.goethe --target de

# a vocabulary app's SQLite backup
python -m pipeline.reword sources/my.backup --target es

# a book you are reading: words you do not know yet, each on a phrase from the book
python -m pipeline.book_import tasks --target en --directory data/en/book_x --epub sources/book.epub
# answer each task_NN.json into an answer_NN.json beside it, then:
python -m pipeline.book_import rows --target en --directory data/en/book_x --subdeck 'Book X'
```

The book importer keeps its questions in its own format and writes no instructions file, so the
script that answers task files cannot read them — they are answered in a session or by hand. Every
other stage below uses the shared format.

### Filling a card from published data

Free, deterministic, no model. Each caches its own lookups, so a rerun costs nothing for the rows
already done.

```
python -m pipeline.dictionaries --target en   # definitions, synonyms, antonyms, corpus sentences
python -m pipeline.frequency --target en      # the frequency band
python -m pipeline.graded_lexicon --target en # the CEFR level, from a published graded list
python -m pipeline.transcription --target en  # the phonetic transcription
python -m pipeline.inflections --target es    # a verb's forms, from the conjugation table
python -m pipeline.context_cards --target en  # which sentence a context card is built on
```

Add `--refresh` to `dictionaries` or `frequency` to redo rows that are already filled.

### The stages that ask a question

These cannot finish on their own. Each writes numbered task files with an `INSTRUCTIONS.md` beside
them, waits for `answer-NN.json` files, and merges those back. Always three steps:

```
python -m pipeline.model --target en --plan                     # write the questions
python -m pipeline.llm data/en/llm --batch                      # answer them through the API
python -m pipeline.model --target en --from-json data/en/llm     # merge the answers
```

`--slices N` splits the questions into more, smaller files. The same three steps work for:

| stage | asks for | task directory |
|---|---|---|
| `pipeline.sense_choice` | which sense a new word's card teaches | `data/en/sense_choice` |
| `pipeline.model` | the fields no dictionary publishes, and simpler wording | `data/en/llm` |
| `pipeline.translations` | the missing example translations | `data/en/examples` |
| `pipeline.relations` | synonyms sorted and glossed | `data/en/relations` |
| `pipeline.senses` | the headword's other meanings | `data/en/senses` |
| `pipeline.primary_sense` | whether a card teaches the sense met first | `data/en/primary_sense` |

**Empty a task directory before planning a new round.** The merge reads every answer file it finds,
so answers to a question no longer being asked get applied again — and the correction you were
making is what gets reverted. Numbering does not save you: a smaller second round leaves the
higher-numbered files of the first one in place and they merge alongside the new ones.

`pipeline.llm` takes a directory, not a `--target`. `--batch` submits everything as one batch at
half price, answered whenever the service gets to it; without it, questions go chunk by chunk and
answer sooner. `./enrich.sh` runs the three stages the build waits on in this way, for a crontab.

### Pictures and recordings

```
python -m pipeline.media --target en              # the curated provider chain
python -m pipeline.media --target en --only audio  # or --only image
python -m pipeline.reword_media sources/my.backup --target es  # media out of the app's backup
```

A searched picture takes four steps, because somebody has to look at it:

```
python -m pipeline.images --target en --plan-queries                       # ask for search phrases
python -m pipeline.llm data/en/image_queries --batch                       # the model answers those
python -m pipeline.images --target en --plan --queries-from data/en/image_queries
# now look at the candidates under data/en/candidates/ and write the answer files
python -m pipeline.images --target en --from-json data/en/image_review
```

Only the query step can go through `pipeline.llm`. Choosing among candidates means looking at
pictures, which the text API cannot do, so that answer comes from a session or from you.
`--retry-rejected` searches again for words a reviewer turned down, which is worth doing after
adding a provider. To check the pictures already on cards: `--audit`, then `--audit-from
data/en/image_audit`.

### A searched picture is looked at before it lands

`pipeline.media` runs a chain of providers declared per language, and it carries only sources
curated before they arrived — whatever the word list shipped, a visual dictionary where a person
chose the photograph. Anything found by searching goes through `pipeline.images`, which offers
candidates and takes a verdict.

That split is deliberate and not a convenience. The moment two paths can write the same picture
cell, the cheap one silently undoes the careful one: a media run here re-pictured the very words a
review had twice cleared as unpicturable. A language with no curated image source simply has an
empty chain, and every picture comes through review.

### Repairing what is already built

```
python -m pipeline.duplicates --target en --dry-run     # fold cards that teach one word twice
python -m pipeline.sense_pruning --target en --dry-run  # drop related words of another meaning
```

Both print what they would do and change nothing until you drop `--dry-run`. Nothing is deleted: a
folded row stays and records which card it went into, so a bad fold is found by reading a column.

### Into Anki

Anki has to be running for all of these.

```
python -m pipeline.anki --target en                  # push the notes
python -m pipeline.anki --target en --design-only    # only refresh templates and styling
python -m pipeline.anki --target en --replace-media  # let the manifest overwrite media
python -m pipeline.ordering --target en              # order the new-card queue by frequency
python -m pipeline.mastered --target en              # suspend the levels you have finished
```

`--url` points at a different AnkiConnect endpoint. To see a card without Anki at all:

```
python -m pipeline.preview --target en --word resilience
```

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

The code is MIT — see `LICENSE`. What it fetches is not covered by that. Every dictionary,
corpus, graded list and image archive it queries has its own terms, and the media manifest
records the licence and attribution for each picture, because that cannot be recovered once the
file is downloaded. One source — the Spanish visual dictionary — states no licence at all, so it
is for personal use only. Check the terms before you publish a deck built with this.
