"""The offline lookup index: the dictionary spans that occur inside one article.

The reader can already resolve a *token*.  It cannot resolve an arbitrary run of
words, because the dictionary never reaches the browser: definitions are baked into
the bundle per token, and a span the segmenter did not produce simply has none.  So
when two words were merged into one token — or a compound was split — the reader's
"widen the selection" handle leads to a dead end: the sheet says there is no meaning
for the range, and the only escape is a paid lookup.

This module closes that at **export** time, where the dictionary is still available
and a lookup costs nothing:

    for every run of 2..MAX_LOOKUP_SYLLABLES syllables in the article,
        if the dictionary knows that run, keep it with its English gloss

Measured on the live articles, a 1,750-syllable article yields ~450 such spans, and
the whole index costs a few tens of kilobytes on a bundle that is already 200-530 KB.
In exchange the reader resolves any highlighted range in one dict lookup, offline,
with no server and no per-tap cost — the same invariant the rest of the page keeps.

Spans the dictionary knows but has no gloss for are kept too, without a definition:
the sheet can then say "the dictionary treats this as one word, and no meaning is
stored" rather than "this is not a word", which is the honest answer and tells the
reader the segmentation is wrong rather than the word being unknown.
"""
from __future__ import annotations

from typing import Sequence

from reading.pipeline.text import Syllable, UNDERSCORE
from reading.storage.dictionary import CompoundDictionary

# Longest run the index will offer.  The dictionary itself caps enforced compounds at
# `max_compound_syllables` (4 by default); a slightly larger window costs almost
# nothing and lets the reader expand a wrongly-split token past the structural cap.
MAX_LOOKUP_SYLLABLES = 6

# How many alternative English senses to carry per span, matching tokens.MAX_EXTRA_SENSES.
MAX_SENSES = 3


def build_lookup_index(
    syllables: Sequence[Syllable],
    dictionary: CompoundDictionary | None,
    max_syllables: int = MAX_LOOKUP_SYLLABLES,
) -> list[dict]:
    """Every dictionary-known span in the article, keyed by syllable range.

    The result is sorted by ``(syl_start, syl_end)`` so the reader can binary-search
    it, and is deliberately compact — short keys, no repeated surface strings; the
    reader already holds the text and the syllable offsets.
    """
    if dictionary is None or len(syllables) < 2:
        return []

    limit = max(2, max_syllables)
    index: list[dict] = []
    total = len(syllables)
    for start in range(total):
        texts: list[str] = []
        for end in range(start + 1, min(start + limit, total) + 1):
            texts.append(syllables[end - 1].text)
            if end - start < 2:
                continue
            form = UNDERSCORE.join(text.casefold() for text in texts)
            # A form can be glossed without being a boundary headword, which is how a
            # word the segmenter produced still gets a meaning.
            senses = dictionary.glosses(form)
            if not senses and not dictionary.is_known_compound(list(texts)):
                continue
            entry: dict = {"s": start, "e": end, "f": form, "n": len(texts)}
            if senses:
                entry["en"] = senses[0]
                if len(senses) > 1:
                    entry["senses"] = list(senses[1 : 1 + MAX_SENSES])
            index.append(entry)
    index.sort(key=lambda item: (item["s"], item["e"]))
    return index
