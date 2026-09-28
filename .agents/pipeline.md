# Vocabulary → Anki pipeline

Turns a personal word list in a foreign language into finished Anki cards: rich, consistent,
and safe to regenerate. Written for Spanish first and since run on three more languages. No stage
names a language; what a language needs of its own is named at the end, under adapting to another
language, together with what running an unfamiliar pair found.

## What a finished card holds

One note per word, with a stable identity so the deck can be rebuilt without duplicating anything:

- the headword, and separately its article, gender and number where the language marks them
- translations into the learner's native language and into English
- a definition written in the target language at roughly A2 level
- three short example sentences with translations
- synonyms and antonyms
- a CEFR level, a frequency band, and the source vocabulary list's own study progress
- a phonetic transcription
- an image, and a synthesised pronunciation
- tags carrying part of speech, level, topic, and whether the word denotes a physical object

## Guiding constraints

**The deduplication key is built from the word itself, never from the source app.** The source
vocabulary list is a one-off import, not a system of record — its internal numbering must not leak
into the deck, or the deck can never be rebuilt from a different source. The key is the headword
together with whatever distinguishes its senses, because a headword alone is not unique: the same
spelling routinely carries unrelated meanings that deserve separate cards. A short native-language
gloss appended to the headword is enough to separate them and stays readable in the card browser.

Anki treats a note type's first field as that key and **silently refuses** a note whose first field
already exists. Refusal and success look identical from the outside, so the import step has to look
the note up first and decide between update and create, and report the two counts separately.
Re-running the import is a normal event, not an accident.

**Dictionaries decide meaning; the model only reshapes it.** A published dictionary entry is
fetched first, and the model is asked to restate it more simply — never to invent one. The model
is trusted only for judgements no dictionary publishes: how hard the word is, whether it names
something photographable, and example sentences where no corpus had any. This is the single
biggest lever on card quality, because it removes the failure mode where a plausible but wrong
definition reaches a card and is memorised.

**Each stage caches its own output on disk, keyed by its input.** Network fetches and model calls
are both expensive and both flaky; a re-run must cost nothing for the words already done, and a
crash halfway must never mean starting over. This also makes it cheap to change one stage and
replay the rest.

**A stage writes the whole table back, so it must never drop a column it does not know about.**
Each stage holds the set of columns it was started with. A long one — a media fetch, a lookup over
every verb — can still be running when a new column is added, and when it finishes it rewrites the
table in the shape it remembers. Any column added meanwhile disappears silently, along with
whatever filled it. Carrying unknown columns through on write costs nothing and removes the whole
class of loss; the alternative is remembering never to change the schema while anything is running.

**A stage fills cells; it does not write them.** The difference is invisible until two passes
answer different questions about the same row. A merge that writes its column unconditionally
turns "this pass knew nothing about that word" into an empty value, and the cell still looks
filled because the separators between the empty parts remain. Roughly seven hundred rows of work
were erased that way, and the erasure was only found because a count that should have gone up
went down.

Two rules follow. A merge writes a cell only when it has something to put in it, and it keeps
what is already there for the parts it says nothing about. And a stage decides what is left to do
by reading the table, never by reading its own answer files — those are rotated as the work is
resliced, so a question already answered looks unanswered and gets asked again.

**A failure of one word never aborts the run.** Dead dictionary entries and refused model batches
are recorded and skipped. Coverage is reported as a count at the end, so gaps are visible rather
than silent.

**Example sentences stay index-aligned across the language columns.** The Nth line of the target
language column, the native translation column, and the English column always describe the same
sentence; a missing translation is an empty line, not a shifted one. Cards render these side by
side, so a silent shift produces a card that teaches the wrong pairing.

Alignment is fragile enough that reading or writing those columns belongs in one place that every
stage calls, rather than in each stage's own string splitting. Two failures make the case: zipping
the columns together truncates every row to its shortest column, silently dropping sentences whose
translation is missing; and appending sentences from two sources — one translated into the learner's
language, one into English — produces a column pair that is aligned and yet gives every sentence only
half its translations. Neither shows up as an error; both show up as a card that looks finished.

**Pronunciation is never written by the pipeline.** Audio is synthesised inside Anki, where the
voice can be changed later without regenerating any data.

## Stages

### 1. Acquire the word list

Flatten whatever the words come from into a tab-separated table, one row per word. Tabs suit this
better than commas because example sentences are full of commas and quotes.

**Treat the source as a plug-in, not as the foundation.** A vocabulary app's export is a lucky
starting point, not the normal case: for the next language there may be nothing but a bare list of
words. Every later stage must therefore work from the headword alone, and treat everything else the
source happened to provide — translations, part of speech, transcription, topics, frequency,
study progress, media — as a bonus that saves a fetch. Whenever a stage cannot get by on the
headword alone, that is a design error in the stage, not a missing feature in the source.

Decide the scope here rather than later: a frequency band, the words already being studied, and
any hand-added list are usually the right first deck. Importing everything at once produces a deck
too large to ever finish.

Carry across everything the app already knows — transcription, part of speech, topic categories,
frequency band, study progress, whether it holds an image. This material is free and better than
anything generated later.

Columns that no source can fill are created empty at this stage, so the table's shape is fixed
from the start and later stages only fill cells.

**Collapse the source list's own duplicates here**, before anything is paid for. A hand-maintained
word list accumulates entries for words the built-in lists already covered, and those copies are
poorer — no article, no part of speech, no transcription.

Group candidates by the **bare** headword, with any article stripped. Grouping on the full headword
looks more precise and silently defeats the whole step: the poor copies are missing exactly the
article that grouping would key on, so they never meet their rich twin. Within a group, two rows
are the same word when their translation sets overlap and different words when they do not — so
grammatical-gender pairs that share a spelling stay apart on their unrelated meanings, which is the
right outcome and needs no special case. Overlap has to be treated as transitive, or a chain of
three entries under-merges.

Merge into whichever row carries the most data, backfilling empty cells from the others. Backfill
the aligned example columns as one unit, never cell by cell, or a sentence ends up paired with the
translation of a different sentence. Deciding any of this later means paying twice for the same
enrichment and shipping two half-empty cards for one word.

### 2. Enrich from dictionaries

Deterministic, free, no model involved. For each word, query in order:

- a **general dictionary API** of the target language, for the definition, synonyms and antonyms
- the language's **Wiktionary**, as a second source for whatever the first lacks — it covers
  colloquial and regional vocabulary that formal dictionaries omit
- a **sentence corpus** for example sentences with ready translations, but only for words that
  arrived without examples

Sense selection is the hard part and cannot be solved here. A dictionary returns many senses in the
target language; the row identifies its sense by a translation in the learner's language. Matching
those mechanically means matching meaning across two languages, which no amount of string handling
achieves. Filtering by part of speech and taking the first survivor — the obvious approach — quietly
gives the wrong sense whenever the headword is polysemous, and gives two rows of one headword the
same definition.

So do not decide here. **Carry every plausible sense forward and let the model that already knows the
row's translation choose**, then keep the chosen one as the audit trail. Taking one sense early looks
tidier and destroys the information needed to correct it later.

**Wiktionary editions differ in where an entry states its meaning, not only in its templates.**
Some mark a sense in the line itself, so the senses can be picked out wherever they stand. Others
write every list — senses, examples, etymology, idioms — with the same line syntax and say what
the list is only in the block heading above it. Reading the second kind line by line collects all
of them and calls them senses, silently, and the card then defines a word with its own etymology.
So an edition has to declare which of the two it is, and the block-filing kind has to be read
block by block.

Filter corpus sentences hard — four to ten words, everyday vocabulary, preferring those that come
with a translation into the learner's native language. An unfiltered corpus sentence is usually
literary and far above the level the card is for.

Keep the dictionary's own examples in a separate column from the beginner-level ones. They are
useful for review but are written for native speakers and should not land on a beginner card.

**A word that arrives without a translation has nothing anchoring its sense, and the dictionary
will not supply one.** A word list from a source app carries a translation per entry, and that
translation silently decides which sense the card teaches — everything downstream is written to
match it. A word list that carries only headwords gives that decision to whatever the dictionary
happens to list first.

That is not a safe default. A dictionary orders an entry's senses historically, not by how often
each is used, so its first sense is regularly the oldest, the most technical or the most regional
one. Measured on one deck, taking the first sense put the wrong meaning on one imported word in
ten: the card for "card" taught a carved ornament on a building, the card for "mobile phone"
taught the adjective "movable", and the card for "aeroplane" taught a species of swallow. None of
this is visible downstream — the translation, the examples and the picture all get written to
match the wrong sense, so the card is internally consistent and reads as finished.

So for a bare headword the sense is a judgement, and it has to be made explicitly: show every
sense the entry lists and ask which one a learner meets first. Ask it as a question about
ordinary speech, not about precision or history.

Correcting a sense is not an edit to one cell. Everything already on the card was written to
agree with the sense being replaced — the translation, the translated definition, the example
sentences, and any picture chosen to show it. Leave those and the card argues with itself: a card
correctly defining an accident illustrated by "the colour of the table is an accident", a card
teaching the aeroplane with a sentence about the bird of the same name. The card reads as
finished, and the contradiction is only visible to someone who reads both halves.

So a sense correction has to clear what depended on the old sense and let the stages that own
those fields fill them again. The rule generalises past this one stage: whenever a decision that
earlier work was built on is revised, that work is stale, and the fix is to empty it rather than
to leave it and hope.

Two things keep that judgement safe. Default to leaving the card alone — replacing a sound card
is worse than keeping a doubtful one, and roughly nine in ten need nothing. And accept "none of
them": an entry can simply lack the everyday sense, holding a homonym or a set of narrow
extensions instead, and inventing a replacement from a list that does not contain the answer is
worse than recording that the source failed.

### 3. Enrich with the model

One request per batch of words, with a schema-constrained JSON response so nothing needs parsing
out of prose. Roughly twenty words per batch balances cost against the risk of losing a whole
batch to one bad response.

The prompt states the role — building flashcards for a speaker of the learner's native language —
then defines each field as a rule rather than an example:

- the **definition** is one sentence in the target language, at A2 vocabulary, defining the sense
  that matches the supplied translation, never using the word itself or anything sharing its root.
  When a dictionary definition is supplied it is authoritative on meaning and is only restated
  more simply.
- the **level** is where a learner normally meets the word; the frequency band is a hint, not a rule
- **is_object** is true only for things that could be photographed — this is what decides which
  cards get an image later
- **examples**, **synonyms** and **antonyms** are requested per word, only where the earlier stages
  left a gap, so the model never overwrites dictionary data

Each word in the batch carries an explicit list of what is still missing. Answers are matched back
by identifier, never by position.

The model's simplified definition becomes the card's definition; the dictionary's original wording
is preserved in its own column so a bad simplification can be spotted and replayed.

Tags are assembled at the end of this stage, since level and object-ness only exist by then. Anki
splits tags on whitespace, so every tag has to be a single token; hierarchical tags with a
separator keep the browser sidebar navigable.

### 4. Complete the example translations

Every sentence a card shows needs a translation into each language the card displays, and the earlier
stages guarantee no such thing: a corpus supplies whatever its contributors happened to write, and the
model is asked only for the learner's own language. Left alone this yields cards where some sentences
are glossed in one language and some in another, which reads as a bug even though every column is
correctly aligned.

Rank each row's sentences before filling anything: fully translated first, then by how close the
sentence is to the length a beginner can read. Ranking rather than discarding keeps the long tail
available while making the card's choice explicit in the file — and it is what lets the fill target
only the few sentences a card will actually show, instead of every sentence ever collected.

Order matters between the two halves of this stage. Reorder the columns first and write them back,
then address sentences by line number; asking for translations before the lines stop moving means the
answers apply to the wrong sentences.

### 5. Gather media

Pronunciation and images, collected before the import so they land with the notes rather than
needing a second pass over the deck.

**Pronunciation is the default and a picture is asked for.** Every word wants a recording and most
words want no picture — the word names nothing anyone could photograph, and the lookup is the
slowest thing in a run. Making pictures the default spends the longest part of every build on the
answer "none of these mean the word".

This stage is a **chain of providers in priority order**, declared per language in its config, all
writing into one directory and one manifest keyed by the card key. Each provider fills only the
cells still empty, so adding a provider later tops up the gaps without disturbing what is already
there, and re-running is free.

The providers worth having, best first:

- **Whatever the source supplied.** A vocabulary app that shipped its own images and recordings is
  free and needs no lookup, which is why it goes first. It is not, however, trustworthy: a third of
  one app's pictures turned out to show a neighbouring thing rather than the word — a photographer
  for "impressed", the Great Wall for "big", the same stock gesture on three different words, and
  the very same image on "six" and "sixth". Treat the source as a fast provider, not a verified
  one, and put its pictures through the same review as a search result.
- **Wiktionary recordings for audio.** Real speakers, not synthesis, and available for any language
  with a Wiktionary edition. One caution: a page carries recordings for every language that shares
  the spelling, so files must be filtered by a language marker in the filename or the deck acquires
  a neighbouring language's pronunciation.
- **A photo search for images, queried in English.** General image search indexes English far
  better than anything else, so query with the pivot translation rather than the headword. Record
  the licence and attribution in the manifest — most freely reusable photos require attribution and
  that information cannot be recovered once the file is downloaded.

  Two things about this kind of provider are easy to get wrong and both produce cards that look finished
  and teach the wrong thing. The **query** cannot be a bare dictionary word: archives are captioned
  in idiom, so "arm" retrieves coats of arms and armed forces, "seal" retrieves the animal. Two or
  three words naming the sense — written by something that knows which sense the card teaches —
  changes the result completely. And the **first result is not an answer**: take several candidates
  and have them looked at, because ranking is text relevance and text relevance is exactly what the
  idiom problem defeats. Prefer whatever free archive is a stock-photo library over a general one;
  its captions describe a plain subject plainly, which is what a card needs.
- **An encyclopedia's page image, last.** Precise for places and species, and actively misleading
  for ordinary vocabulary: an encyclopedia illustrates the article, not the word, so "mouth" comes
  back as a crocodile's jaws.

**A searched picture reaches a card only after someone has looked at it.** This is a constraint on
the pipeline's shape, not advice. The moment two paths can write the same picture cell — a provider
chain that takes the first downloadable hit, and a reviewed stage that chooses among candidates —
the cheap path silently undoes the careful one. It happened here: a media run re-pictured the very
words a review had twice cleared as unpicturable, because the provider chain neither knew nor could
know that a decision had been made about them.

So the provider chain carries only sources curated before they arrived: whatever the word list
shipped, and a picture dictionary where a person chose the photograph to mean the word. Anything
found by searching belongs to the reviewed stage. A source that needs judgement and a source that
does not must not share a route.

A language may have no curated image source at all — no app export, no visual dictionary worth
trusting — and then the chain is simply empty and every picture comes through review. That is the
rule working, not a gap to fill: adding a search provider to the chain to make it non-empty is
exactly the shortcut the rule exists to forbid.

**A rejection is a decision and has to be stored like one.** Clearing the picture cell is not
storing it: an empty cell means "no picture", and "no picture because nobody has looked yet" and
"no picture because someone looked at twenty and none of them meant the word" are opposite states
that then look identical. The stage picks its work by that cell, so every rejected word is queued
again on the next run, re-searched, re-reviewed and re-rejected — the cost repeats forever and the
reviewer's effort buys nothing permanent.

Write the verdict onto the row instead, with the reason the reviewer gave. The reason matters more
than the flag: it is what lets a later reader tell "nothing photographable exists for this word"
from "the search phrasing was wrong", and only the second is worth retrying.

Keep the decision re-openable. A verdict records what the available sources could offer at the
time, not a fact about the word, so adding a provider makes some of these worth asking again —
there must be a way to say "search the rejected ones too" without hand-editing the table.

Store it where the stage reads it, which is the row. Answer files from a review round rotate and
get cleaned up, so a stage that consults them to decide what is settled loses its memory the moment
they are gone.

**Search for images only for words that name something photographable**, using the judgement made
earlier: hunting for a picture of an abstract noun wastes the lookup and usually returns something
that has to be decoded rather than recognised.

One class of word defeats the search however well it is phrased: those naming a role in a
relation rather than a thing. A stepfather, a half-brother, a godparent and a husband look like
any other person in a photograph — what makes the word true is a fact about two people that the
frame cannot hold. Every candidate for these came back showing a person, and every one was
rightly rejected. Skip them: the search costs a lookup and the answer is always no.

That judgement is a good rule for *searching* and a poor filter for *keeping*. Measured against a
review of every picture already in one deck, words marked photographable kept their picture 81% of
the time and abstract ones 63% — enough of a gap to guide a search, nowhere near enough to delete
by. Plenty of abstract words do have a picture that works. Only looking at the picture settles it.

Whatever has no recording after this stage gets synthesised speech inside Anki later — cheap,
regenerable, and the right fallback rather than the first choice.

### 6. Prepare Anki

Install the AnkiConnect add-on, which exposes a local HTTP API on Anki's own machine while Anki is
running. Create the note type once, with one field per column the cards render, plus an empty
pronunciation field and an empty image field for the later stages to fill.

### 7. Push the notes

A small script maps the enriched table onto the API's note format and sends it in batches, uploading
the gathered media files alongside. It must be idempotent: look the note up by its key first, then
update or create. Running it twice is a normal event, not an accident.

**A media field is written only when the note's own is empty.** Audio and images arrive from several
places across the deck's life — this pipeline, a text-to-speech pass inside Anki, a hand-picked
replacement — and a re-import that overwrites them silently destroys work that cannot be regenerated
from the table. Reading the current field values before updating is the whole cost of avoiding that.

That rule also blocks deliberate removal, since it cannot tell a picture judged wrong from one added
by hand. So removal is a separate, explicitly requested mode in which the manifest wins outright,
empty cells included. Making it the default would quietly undo every hand edit on the next run.

Report what was added, what was updated, and what was rejected. Anki rejects a note whose first
field duplicates an existing one, and a silent rejection looks exactly like success.

### 8. Order what the learner meets first

A deck decides the order of study twice, and left alone it gets both wrong.

The coarse order is by level. Anki gathers new cards subdeck by subdeck in name order, and CEFR
level names already sort into study order, so putting each word in a subdeck named for its level
buys the whole progression with no other machinery. Without it a learner meets levels shuffled
together from the first day, and — worse — every later import lands behind everything already
there, so newly added beginner words wait out the entire existing deck before they appear. A
learner reads that as "the import did nothing".

The fine order is inside one level, and it matters more than it looks: a level holds well over a
year of study at a normal daily limit, so the order within it decides most of what is actually
seen. By default that order records how the deck was built, which is meaningless to a learner.
Frequency is the ordering worth having — within a level, the commoner word comes first, and words
the frequency lists never ranked go last, since an unranked word is either rare or too specialised
to have been counted.

Both cards of a word take the same position; they are one word met two ways, and separating them
lets the deck's own sort order decide which side leads.

**Rewrite a position only for a card nobody has started.** A card in review keeps its schedule in
the same place the position lives, so rewriting it discards review history. Preserve the current
relative order within a frequency band as well, so re-running after an import slots the newcomers
in rather than reshuffling a deck the learner is part-way through.

This runs after the push, because positions exist only once the cards do.

### 9. Fill the remaining pronunciation

Synthesise speech inside Anki for the notes still lacking audio. It runs after import because the
audio derives purely from the headword, so regenerating it is cheap and it needs no data the deck
does not already hold.

### 10. Design the card template

Front and back templates plus styling, kept in the note type. Worth doing last, when the real data
is visible: what looks fine with a one-line definition falls apart with a four-line one. Design for
the longest row in the deck, not the average, and check both light and dark mode.

## A stage's answer files outlive the question they answered

Every model-backed stage works the same way: it writes numbered task files, someone answers them
into numbered answer files beside them, and a merge step reads every answer file in the
directory. Run the stage a second time and the old answers are still sitting there.

That is not a stale-data nuisance, it is a silent revert. The merge reads them as current, so
answers to a question that is no longer being asked get applied to the table again. Worse, the
instruction that makes a long run survivable — read your answer file first, keep what is already
there, carry on from the first unanswered row — reads a whole stale file as finished work and
writes nothing at all. The stage reports success, the answers look complete, and the table has
quietly been put back the way it was.

It fails in exactly the case where it hurts most: rerunning a stage to correct something. The
correction is what gets reverted.

So clear or archive a stage's answer files before planning a new round. Numbering does not save
you — a smaller second round leaves the higher-numbered files from the first round untouched and
they merge alongside the new ones. Match the answers to the questions by emptying the directory,
not by trusting the names to line up.

## Adapting to another language

**The prompt is parameterised, not copied.** Every language name in it — the one being learned, the
learner's own — is substituted from a small per-language config file, so one prompt serves every
deck. Copying the prompt into a per-language directory looks tidier for about a week; then a fix
lands in one copy and the others quietly keep the bug. The prompt is the highest-value artefact
here and the one whose defects are hardest to see in the output, so it stays single-sourced.

The same argument decides the column names: the pipeline works in terms of roles — target language,
the learner's own language, an optional pivot language — and renders the actual column headings
from the config, so the exported file still reads as concrete named columns while the code never
mentions a specific language. Field names sent to the model stay role-named for the same reason.

Directories are split only where the code genuinely differs: the dictionary adapters, since every
language has its own APIs, and the data and caches, which are per-language by nature.

Unchanged across languages: the stage order, the deduplication rule, the dictionaries-then-model
split, the caching, the alignment rule, the note type, and everything from the import step onward.

What has to be re-chosen per language:

- **The dictionary sources.** Availability varies sharply. Expect to find a formal academic
  dictionary with an API and a Wiktionary edition; expect *not* to find a learner's dictionary with
  an API, which is why the model does the simplification.
- **The sentence corpus coverage.** Translation coverage into the learner's native language is
  usually far thinner than into English; check before relying on it.
- **The CEFR source.** Look for an official word list per level before settling for a judgement.
  German has one — the exam board publishes a list per level, and each is cumulative, so a word's
  level is simply the first list holding it. That is a published fact rather than an estimate, and
  it is worth real effort to find: the same document also states the article, the plural, a verb's
  principal parts and an example sentence per entry, all written for the level. Where no such list
  exists the level stays a model judgement anchored on corpus frequency, and should be called
  approximate.

  A published list is usually a typeset PDF rather than data, which changes how it is read but not
  what it is worth. Extract the text with its coordinates and separate the columns by them —
  and classify each *word* by its own position, never the block it landed in, because the extractor
  merges a headword with its neighbouring example often enough that a block-level rule reads the
  pair as one headword. Two details cost a re-run each: a hyphen at a line end is a broken word
  only when it hangs off a word and a lowercase word follows, since alone it is a noun's plural and
  before "and" or "or" it stands for an omitted compound; and a printed plural like `¨-e` has to be
  expanded to the form a learner would say, or the card teaches an abbreviation.
- **The morphology carried in its own columns.** Articles, grammatical gender and number are
  language-specific; split them out of the headword rather than leaving them glued to it, so cards
  can drill them separately.
- **The transcription source.** Best taken from the source word list if it carries one, since
  generating it is unreliable; otherwise the language's Wiktionary usually publishes it.
- **The media providers and their order.** Only the source-supplied one is language-specific in
  kind; the rest differ in how well they cover the language. The filename markers that identify a
  recording as belonging to this language and not a neighbour are per-language and easy to get
  subtly wrong.

Nothing in the pipeline may assume a source app exists at all. A bare list of words has to be a
valid starting point, because for most languages that is all there will be.

### What running a second and a third language taught

The rule was stated from the start and the stages added later quietly broke it. Building ten cards
for a reader of another language, and then ten cards of another target language, found every break
in an afternoon — and none of them by failing. Every stage ran and reported success.

That is the finding worth keeping: **a stage that holds a fact about one language does not break on
another language, it answers wrongly.** Nothing reports it, because plausible output is what these
stages are built to produce. Running one small deck in an unfamiliar pair is therefore the only
test that finds them, and it is cheap.

Three failures were worse than a wrong answer, and each has a general shape.

**A reduction that loses everything compares as a perfect match.** Folding two cards that teach one
meaning worked by stripping a definition down to the letters it recognised. For a script it did not
list, that is no letters at all — and the standard string comparison calls two empty strings
identical. So the deck did not fold a few cards wrongly; every card matched every other. Whenever a
comparison runs on normalised text, the empty result is not a value — it means the comparison had
nothing to work with, and it has to be answered with "unlike", never with the number the library
returns.

**Filtering an answer after the fact is not the same as asking the right question.** The corpus was
asked for a language's sentences and its translations were then filtered against two fixed codes.
On a deck those codes did not match, every sentence was dropped and the result read as "the corpus
has nothing" — the opposite of true. Worse, on a deck where one code happened to match the wrong
role, the column a card labels as the reader's own language filled with a third language's text.
A filter keyed on anything but the role is a silent mislabel waiting for its deck.

**A cache keyed on less than its input serves the wrong answer.** Dictionary entries were cached
per word, not per edition, so pointing a deck at a different edition returned the stored markup of
the previous one. The stage that refuses to read an edition it has no dialect for is loud and
correct, and the cache walked straight past it: no error, no definitions, a finished-looking deck.
A cache key has to carry every input that changes how the stored value is read, and an entry stored
before a key grew has to be treated as a miss.

Two smaller lessons came with them. Unwrapping markup in one pass leaves whatever wrapped
something else, and the stage downstream throws away what still carries markup — so the loss shows
up as missing content, not as visible debris. And an edition written in a *third* language looks
like a free way past a missing dialect and is a trap: such an edition translates foreign words
rather than defining them, so the card's definition arrives as a one-word gloss and the founding
rule — a dictionary decides meaning — is quietly gone.

### Where a language's own facts belong

The cause was the same every time: a list of words, endings or phrases that belongs to one language
was written beside the code instead of beside that language's other facts. So the test for a new
stage is simple. If it needs to know *something about a language* — which of its words carry no
meaning, which endings a reader passes through, how a definition of it is usually phrased — that
knowledge is a config entry, and a literal list in the stage means the stage has silently become
single-language.

Two things make this safe to get wrong at first.

**A language that states nothing must still build.** Every one of these entries is optional, and
without them a stage falls back to matching the written form: fewer sentences qualify, fewer cards
fold, and nothing is accepted wrongly. Degrading towards doing less is what lets a new language
start on the day it is added rather than after its lists are written.

**Frequency is a fallback for a stated list, not a replacement.** The commonest words of a language
are mostly its grammar, which is tempting: one frequency table covers every language for free. It
is not the same list, measurably. Swapping a hand-written English list for the top of the frequency
table cost the existing deck a few hundred sentence cards, because a frequency table counts plain
vocabulary — the words for a day, a person, to know — as grammar, while missing modals and
relatives that carry no meaning at all. Errors in both directions, neither visible. So the derived
list is what a language gets before anyone writes its own, and a deck that matters gets a written
one.

Also worth separating: two lists that overlap almost completely can still answer two questions. The
words that add nothing to a *sentence* and the words every *definition* reaches for are nearly the
same set and not the same set, and merging them moved cards in a deck that was supposed to stand
still.

### A learner's progress is not a fact about the language

One config file per language held both what the language is and how far one learner had got: which
levels they had finished, what their decks were called, which cards they still needed. The second
learner of that language inherits all of it — a beginner gets the advanced deck's name, their first
three levels suspended before they start, and the card that would have taught them missing. The
level even reached a prompt, telling the model to pitch its answers at a learner who did not exist.

So the two are separate files, merged in order, and the learner's one is keyed on both languages.
A level a prompt needs is derived from the levels marked finished rather than written down twice.

### What is still tied to one language

Only entry points, and only the optional ones. The exam board's word list reader is for one
language by nature. The book importer and the vocabulary-app backup importer are written through
and through for one pair — their alphabets, their endings, their column names. Nothing depends on
any of them: they are ways for words to get *in*, and a plain list of words is always a valid way
in, so another language loses nothing but the shortcut.

One more of these was found by rendering a card for a reader of a right-to-left script, and it is
worth the telling because it hid in a place nobody would look for a language assumption. **A
filename built from a key carries the key's languages.** The key ends in a gloss in the reader's
own language, and making it filesystem-safe meant transliterating it — with a table that described
two scripts. For a third script the gloss contributed nothing at all, so two senses of one headword
asked for the same file. That one failed loudly, because a guard checks the names are distinct
before anything is fetched, and the lesson is the guard rather than the table: the table will always
be missing a script, so what saves the run is refusing to proceed on a clash, and what fixes it is
giving such a key a short tail derived from the key itself rather than describing one more alphabet.

The same render showed the other half of reading right to left: the text was correct and sat in
blocks laid out for the other direction. Every block that holds the reader's own language now takes
its direction from its own first letter, which costs nothing for a reader whose language runs the
same way as the card. Easy to half-do — the translations were covered and the section headings and
the card's own question were not, which is exactly the part a reader looks at first.

One gap degrades rather than breaks: card section headings exist for a handful of reader languages
and fall back to English for the rest. A card in the wrong language for its headings is readable;
a card teaching the wrong thing is not, which is the right order to fix them in.
