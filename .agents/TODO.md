# Wanted next

Ordered by how much each would improve a card, not by effort. Everything here was agreed as
worth doing; nothing is a maybe.

## Inflection on the card — done for verbs

A verb card carries only its infinitive, which is the one form a sentence rarely uses. The
forms exist in the conjugation tables Wiktionary renders, and they are the same tables for
every language it covers, so this is one stage rather than a Spanish special case.

Keep what reaches the card short. The full table is sixty forms and unreadable on a phone; the
present tense plus a line of principal parts — one preterite, one imperfect, one future, the
gerund and the participle — is what a learner actually reaches for.

Nouns and adjectives have their own inflection and the same argument applies to them, but the
verb is where the gap hurts, so it goes first.

## Corpus sentences come back glossed in the wrong languages — half done

The filter half is fixed: the translations a card keeps are now matched on the role they fill, so a
sentence glossed in a neighbouring language is dropped instead of being written into the column the
card labels as the reader's own. That was not a gap but a mislabel, and it was reaching cards.

What is left is the coverage half. The corpus is still asked for a language's sentences without
being told which translations are wanted, so it answers with whatever its contributors happened to
write and the useful ones are picked out afterwards. Stating the wanted languages in the request
would raise coverage rather than correctness. Worth knowing before starting: the request is part of
the cache key, so changing it makes every deck pay for its corpus lookups again.

## Language facts moved out of the stages — done

The endings, grammar words, filler words and definition openers the shared stages carried now come
from the language's own config, and a language that states none is matched on the written form. The
prompts no longer name a language or a learner's level. The learner's own progress moved into a file
of its own, keyed on both languages, so the next learner of a language does not inherit the first
one's deck.

The reasoning, and the three failures that were worse than a wrong answer, are written up in the
pipeline brief under adapting to another language. Two things left undone on purpose: the book
importer and the vocabulary-app backup importer stay single-language, because nothing depends on
them and a plain word list is always a valid way in; and card section headings cover four reader
languages and fall back to English for the rest.

## Which prepositions a word takes

Knowing a word without knowing that it demands a particular preposition means building the
sentence wrong every time: to dream is `soñar con`, to think is `pensar en`. Dictionaries state
this inside their examples rather than as a field, so it has to be extracted rather than looked
up. The words it matters for are verbs and a handful of adjectives, so the work is bounded.

Collocations belong here too — the two or three words a headword habitually appears beside.
A corpus gives these; the risk is dumping frequency noise onto the card instead of the two
pairings worth memorising.

## Synonyms that are not synonyms — done

Sorting the lists by literal against figurative turned up a third group nobody was looking for:
entries that share no sense with the headword at all. One card offers `siempre` as a synonym of
`jamás`, which is its opposite. These come from taking a dictionary's related-words list at face
value, where proximity in an entry does not mean interchangeability.

The same pass that judges figurativeness could reject these outright, and should — a wrong
synonym is worse than a missing one, and unlike a merely figurative one it teaches something
false rather than something incomplete.

## Where the word is used

`coche` in Spain is `carro` across most of Latin America, and a card that teaches one without
mentioning the other teaches half a word. Register matters the same way: a colloquialism the
learner drops into the wrong conversation is worse than a word they never learned.

Wiktionary marks both, as labels on the sense rather than as structured data, so this rides
along with whatever already reads those entries.

## False friends

`embarazada` is not embarrassed and `éxito` is not an exit. There are few enough of these that
the model can be asked directly, per word, whether the learner's own language has a lookalike
with a different meaning — and a card that warns about it is worth more than three examples.

## A hint on the recall card — done

Producing a word from its translation with nothing to go on is where a session stalls. The
first letter, or the number of letters, is enough to convert a blank into a recall — the
standard trick, and cheap, because both are derivable from data already on the card.

Worth doing as a hidden field the learner can reveal, not as something always visible, or it
stops being recall.

## Pictures for the words that have none

Forty photographable words ended up with no picture because the free keyless archives had
nothing plain enough for them — hands, bodies, a ball. A stock library with a free API key
covers exactly these. The provider is written and disables itself without a key; getting the
key is the whole task.

## Subdecks — done, split by level rather than by frequency band

Done as a split by CEFR level, not by frequency band as first sketched here. Level names sort
into study order on their own, so Anki's subdeck-by-subdeck gathering delivers the progression
for free, and a level is the unit a learner actually thinks in — they can hold a whole level back
once they know it, which a frequency band does not express.

The frequency bands were not wasted: they became the ordering *inside* each level, where they
answer the original complaint that the first hundred words should not queue behind the five
thousandth. Both halves are described in the pipeline brief.

## Words whose everyday sense the dictionary does not list — done

Seven cards were rebuilt from scratch: `ese`, `horario`, `suizo`, `extrañar`, `chileno`,
`colonial`, `despedida`. Three of the original ten turned out to be sound already.

The check that found them also grew into a wider one. The sense-choosing pass compares the
dictionary's senses against the card's *definition* and never looks at its *translation*, so a
card can pass with the two halves naming different things — a definition describing a baseball
player under a translation reading "troublemaker". Comparing the two across every word the pass
had left alone found seven such cards in 652, about one in a hundred.

Worth knowing for the next language: the two halves of a card are written by different steps
against different inputs, so nothing makes them agree unless something checks. The check is cheap
— two short strings and one question — and it belongs after any pass that rewrites one half.
