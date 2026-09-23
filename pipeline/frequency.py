"""Fill the frequency_band column from wordfreq's own corpus, not a source app's export.

An entry point sets this column only from the source app's own frequency categories
(top100, top1000, top3000, top5000), so a word added by any other route — a dictionary
lookup, a media provider's own vocabulary, a hand-added list — is left with an empty
cell. The model stage is told the band is a strong hint when judging what level a word
belongs to, so an empty band leaves that judgement unanchored. Wordfreq bundles its own
corpus data for every language this project supports, so it can answer without a source
app or a network call at run time.

A single pass, unlike the model-backed stages: there is no model in the loop, so no
--plan/--from-json split — read the table, classify, write it back.
"""

import argparse
import collections
import pathlib
import typing

import wordfreq

from . import language_config

CARDS_FILENAME = 'cards.tsv'
# Wide enough to cover every band below; wordfreq itself caps out well past this.
LIST_SIZE = 30000
# A word neither ranked in the corpus nor scored above zero by it: the corpora do not
# really attest it, which is exactly what a learner does not need.
RARE_BAND = 'rare'

# Rank ceiling to band name, tightest first. The first three names are kept exactly as
# the source app spelled them, so old rows stay comparable with ones computed here.
BAND_BOUNDARIES = (
    (100, 'top100'),
    (1000, 'top1000'),
    (3000, 'top3000'),
    (10000, 'top10000'),
    (30000, 'top30000'),
)

MAX_DISAGREEMENT_EXAMPLES = 5


class FrequencyBands:
    """Rank and Zipf-score lookups built once from wordfreq's corpus for one language."""

    def __init__(self, *, language_code: str):
        ranked = wordfreq.top_n_list(language_code, LIST_SIZE)
        self._language_code = language_code
        self._rank_by_form: typing.Dict[str, int] = {form: rank for rank, form in enumerate(ranked, start=1)}
        # A headword that is not a single corpus token — "can opener", "decree nisi" — has
        # no rank of its own, but wordfreq still scores it. For those, the Zipf value observed
        # at each rank boundary in this same list stands in for the boundary.
        self._zipf_thresholds = [
            (band, wordfreq.zipf_frequency(ranked[limit - 1], language_code))
            for limit, band in BAND_BOUNDARIES
        ]

    def band_for(self, *, word: str) -> str:
        form = word.strip().lower()
        if not form:
            return RARE_BAND
        rank = self._rank_by_form.get(form)
        if rank is not None:
            return self._band_by_rank(rank=rank)
        score = wordfreq.zipf_frequency(form, self._language_code)
        if score <= 0:
            return RARE_BAND
        return self._band_by_score(score=score)

    @staticmethod
    def _band_by_rank(*, rank: int) -> str:
        for limit, band in BAND_BOUNDARIES:
            if rank <= limit:
                return band
        return RARE_BAND  # unreachable while the rank lookup only ever holds LIST_SIZE forms

    def _band_by_score(self, *, score: float) -> str:
        for band, threshold in self._zipf_thresholds:
            if score >= threshold:
                return band
        return RARE_BAND

    def zipf_for(self, *, word: str) -> float:
        """How common a word is on wordfreq's own scale, where each whole step is ten times rarer.

        Unlike `score_for` this is comparable between two words rather than merely orderable, which
        is what a caller needs to ask whether one word is far rarer than another. A phrase is worth
        no more than its hardest word, so it scores as its rarest part.
        """
        forms = word.strip().lower().split()
        if not forms:
            return 0.0
        return min(wordfreq.zipf_frequency(form, self._language_code) for form in forms)

    def score_for(self, *, word: str) -> float:
        """A single number that orders words from most to least useful; higher sorts first.

        Reuses the same rank and Zipf lookups `band_for` uses, but hands back a bare number
        instead of a bucket name, for a caller that wants to rank individual words against
        each other rather than sort them into shelves. A ranked word always outranks one only
        found by the direct Zipf lookup, so the two scales never cross.
        """
        form = word.strip().lower()
        if not form:
            return float('-inf')
        rank = self._rank_by_form.get(form)
        if rank is not None:
            return -rank
        return -LIST_SIZE + wordfreq.zipf_frequency(form, self._language_code)


def main_for(
    *,
    language: language_config.LanguageConfig,
    root: pathlib.Path,
    refresh: bool = False,
    limit: typing.Optional[int] = None,
) -> None:
    cards_path = language.data_directory(root=root) / CARDS_FILENAME
    rows = language_config.TsvFile.read(cards_path)
    bands = FrequencyBands(language_code=language.target)

    filled = 0
    counts: typing.Counter[str] = collections.Counter()
    disagreements: typing.List[typing.Tuple[str, str, str]] = []
    updated_rows = []
    for index, row in enumerate(rows):
        if limit is not None and index >= limit:
            updated_rows.append(row)
            continue

        computed = bands.band_for(word=row.get('word', ''))
        existing = row.get('frequency_band', '')
        if existing and computed != existing:
            disagreements.append((row.get('word', ''), existing, computed))

        updated = dict(row)
        if refresh or not existing:
            updated['frequency_band'] = computed
            if not existing:
                filled += 1
        updated_rows.append(updated)

    for row in updated_rows:
        band = row.get('frequency_band', '')
        if band:
            counts[band] += 1

    language_config.TsvFile.write(path=cards_path, rows=updated_rows, columns=language.card_columns)

    counts_summary = ', '.join(f'{band}: {counts[band]}' for _, band in BAND_BOUNDARIES if counts[band]) or 'none'
    if counts[RARE_BAND]:
        counts_summary += f', {RARE_BAND}: {counts[RARE_BAND]}'
    print(f"filled {filled} empty bands across {len(rows)} rows ({counts_summary})")

    if disagreements:
        examples = ', '.join(f'{word}: {old} -> {new}' for word, old, new in disagreements[:MAX_DISAGREEMENT_EXAMPLES])
        print(f"{len(disagreements)} rows carry a band that disagrees with the computed one, e.g. {examples}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--refresh', action='store_true', help="Recompute every row, not only empty bands")
    parser.add_argument('--limit', type=int, default=None, help="Process only the first N rows")
    language_config.add_language_arguments(parser)
    arguments = parser.parse_args()

    root = language_config.PROJECT_ROOT
    main_for(
        language=language_config.language_from(arguments, root=root),
        root=root,
        refresh=arguments.refresh,
        limit=arguments.limit,
    )


if __name__ == '__main__':
    main()
