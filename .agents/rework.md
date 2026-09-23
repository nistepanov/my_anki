# Reworking the English deck

An agreed plan. The parts marked done are built; each moves into the pipeline brief as that brief
is next revised, and this file shrinks by the same amount. Written for English, but nothing in it
is English-specific except where it says so.

Two complaints started it. Synonyms on a card often belong to a different meaning of the
headword. And the deck asked the learner to produce a word from its definition, which is a
strange thing to be asked and not what reading a foreign language feels like.

Underneath both sat a third problem nobody had stated: most of what the model is paid to decide
is already published somewhere for free.

## Synonyms belong to a sense, not to an entry — done

A dictionary entry states its synonyms under the meaning they belong to. The pipeline collected
them from the whole entry at once and handed the flat list to every card built from that
headword. A card teaching the verb "to let something go free" therefore offered three synonyms
of the noun meaning "relief from pain", and a card teaching a gradual decrease offered a
synonym of the grammatical sense of declining a noun.

Every wrong synonym found so far is of this kind. None were invented; all were correct
somewhere else in the same entry. So the fix is not to judge the list after the fact but to
stop flattening it: a meaning carries its own synonyms from the moment it is parsed, and the
card takes the ones belonging to the meaning it teaches.

The entry offers two more things the pipeline was throwing away. Individual synonyms carry a
marker when they are rare, obsolete or archaic, and a meaning carries the same kind of marker
for itself. Both are reasons to drop a synonym, and the second is also a reason not to teach
that meaning at all. A third filter needs no dictionary: a synonym rarer than the headword
teaches nothing, because the learner now has two unknown words instead of one, and a frequency
table settles that without asking anyone.

This retires the pass that asked a model to sort synonyms into good, figurative and wrong.
Figurativeness is stated in the entry as a marker, so it was being bought rather than read.
Register and region — colloquial, dated, British, American — come from the same markers, which
closes a wanted item that was never started.

What the model still has to do is translate each synonym into the learner's own language. That
is a list of single words, not sentences, and costs a fraction of what the judging pass did.

Old rows do not need refetching. The original dictionary wording is kept on every card, so the
meaning it came from can be found again in the cached entry and its synonyms read off.

## A sentence you read, not a blank you fill — done

The deck had two cards that asked the learner to produce a word: one from its definition, one
from a sentence with the word cut out. Producing a word from its definition is a puzzle, not a
language skill, and the gap-fill was the same puzzle with a sentence wrapped around it.

The sentence card is inverted instead. The word stays in the sentence and is marked; the
learner reads the sentence and works out what the marked word means. That is what meeting an
unknown word in a text actually feels like, and it is the only one of the three cards that
practises it.

The card showing the bare headword asks the same question — what does this mean — with less to
go on. So where a word has a sentence worth using, the sentence card replaces it rather than
joining it. A word with no such sentence keeps the bare headword, because half a card is better
than none.

Producing a word from its meaning went entirely. At this level the deck is vocabulary the learner
needs to recognise and will never need to say, and the card asking for production was the one that
felt like a riddle rather than a language.

Which cards a word gets is now stated per language rather than fixed, because the answer depends
on how far along that deck's learner is: the prompt that still teaches a beginner gives the answer
away to someone who already reads the language. Bringing the card back is a line of configuration,
not a code change.

## Choosing the sentence without a model — done

A sentence works for this only when the target word is the one thing in it the learner does not
know. That rule was being applied by a model, one word at a time.

It does not need one. The learner's known vocabulary is the frequency list down to a few
thousand words plus every word the deck has already taught, and a sentence qualifies when
everything in it except the target is in that set. Run over the sentences already collected,
this finds more usable sentences than the model did, for nothing — the model was being cautious
about sentences the rule accepts.

The rule is also honest about its own coverage in a way a model is not: a word with no
qualifying sentence is a word whose examples are too hard, which is a fact worth having rather
than a judgement to pay for.

Among the sentences that qualify, the shortest is the wrong pick — that was learned from the
output. "Is that a lynx?" passes every difficulty test and teaches nothing, while "You have the
eyes of a lynx" teaches the word. So they are ranked by how much they say beside the target,
counting the words that are not grammar, and a sentence surrounded by nothing but grammar is
dropped rather than ranked last. That threshold costs a few dozen cards, including some decent
ones, and is worth it: a card that cannot be answered from its sentence teaches guessing.

## Cards that teach one word twice — done

A headword gets a card per sense, which is right for a spelling carrying unrelated meanings and
wrong for one meaning wearing two grammatical hats. English turns a verb into a noun without
changing a letter, so the deck asked about `escape` twice — getting away, and the act of getting
away — and the second asking taught nothing the first had not.

They are found by comparison rather than judgement: strip the opener that only announces a part
of speech, and a noun's definition and its verb's are the same sentence. The same pass caught two
outright faults, a word split on a distinction that was not one and a word split into two cards
with identical definitions.

Two things the merge has to keep apart, learned by getting it wrong first. Which note survives is
decided by review history alone — folding away a card the learner has been studying throws that
work away. Which wording the merged card shows is a separate question with a separate answer, and
answering both at once left the surviving card stating that a hostage is a woman.

Nothing is deleted. The folded-away row keeps its place and records which card it went into, so a
bad merge is found by reading a column. The note it left in Anki is another matter: Anki holds a
copy the table cannot reach, so that note keeps asking its question until it is deliberately
retired — which costs its review history, and is therefore asked for rather than assumed.

## Levels, other meanings, topics

The same argument retires three more passes.

A word's level was being judged by a model against its definition and frequency. Published
graded word lists state it directly, and where they are silent the frequency band is a better
guess than it looks. The level only decides which subdeck a card lands in and whether it stays
hidden, and the learner studies every level above the ones they have finished — so the
difference between two adjacent levels costs nothing, and paying for precision there was waste.
The lists do not cover the rare end of the deck, so a second list is added beside the first.

The other meanings shown on the back of a card were being chosen by a model from the entry's
list. The entry already states them in order of importance, and the markers described above say
which ones are dead. Taking the first few live ones is as good a choice as was being bought.
The model keeps only the translation of the line it picks.

Topics were being assigned by a model from a closed vocabulary. The vocabulary app the deck was
imported from had already sorted its words by topic, and that data is still in the import. It is
coarser than what the model produced and it is free, and topics only become tags — they decide
neither order nor subdeck.

## What the model is left with

Restating a dictionary definition in simple words, and translating: definitions, synonyms, and
the odd line of another meaning. Everything else now comes from a dictionary, a word list or a
frequency table.

That is the right division. A published definition is authoritative about meaning and a model
is not, which was already the pipeline's founding rule; what this plan does is notice how many
other questions had a published answer too.

## The word list itself

Unfinished, and the one part of this plan with no decision behind it yet.

The list came from a vocabulary app's backup and carries that app's shape: heavily weighted to
nouns, with a long tail of words too rare to meet, a handful of entries that are phrases rather
than words, and a few that are not vocabulary at all. Against that, the words a learner at this
level actually lacks are largely absent.

Adding a published level-graded list as a second source for levels also answers this, because
the same list names the words of each level the deck does not have. That comparison has not been
run yet.
