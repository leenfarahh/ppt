"""Which script a piece of copy is written in, and which way it reads.

Prezlab decks are bilingual and the two halves are not interchangeable. A
Latin typeface has no Arabic glyphs, so setting Arabic in one gets you
fallback shapes or boxes; a column of Arabic aligns on its RIGHT edge, so a
grid measured on left edges cannot see it; and Arabic text in a paragraph that
was never marked right-to-left renders with its punctuation and its numbers in
the wrong places.

None of that is a preference. It is the difference between a deck that reads
and one that does not, and every part of it is decidable from the text itself.

The ranges are the Unicode blocks Arabic actually uses: the base block, the
supplements, and the presentation forms that older tools still emit. Hebrew is
here too -- it is the other right-to-left script a deck might carry, and the
direction half of this is the same for both.
"""

from __future__ import annotations

# Arabic, Arabic Supplement, Arabic Extended-A, and the two presentation-form
# blocks. The last two are legacy but real: a deck round-tripped through an
# older tool comes back full of them.
_ARABIC = (
    (0x0600, 0x06FF),
    (0x0750, 0x077F),
    (0x08A0, 0x08FF),
    (0xFB50, 0xFDFF),
    (0xFE70, 0xFEFF),
)
_HEBREW = ((0x0590, 0x05FF), (0xFB1D, 0xFB4F))

# Enough of the letters have to be right-to-left before the run is called
# Arabic. A single Arabic word inside an English sentence is a quotation, not
# a change of script, and setting the whole paragraph right-to-left for it
# would move the English around it.
# A strict majority, so an even split is not called Arabic. Ties come up in
# real decks -- "NEOM نيوم" is four letters each way -- and the direction of a
# tie is a coin flip, which is not a thing to decide a paragraph's direction
# on.
_MAJORITY = 0.5


def is_arabic(text: str) -> bool:
    """Whether this text is Arabic rather than Latin with a word in it."""
    return _share(text, _ARABIC) > _MAJORITY


def is_rtl(text: str) -> bool:
    """Whether this text reads right to left, in any script that does."""
    return _share(text, _ARABIC + _HEBREW) > _MAJORITY


def has_arabic(text: str) -> bool:
    """Whether a single Arabic letter appears at all.

    The test for typefaces, and it is deliberately not the majority one. A
    Latin face has no Arabic glyphs, so ONE Arabic word inside an English run
    renders as boxes -- how much Latin sits beside it changes nothing about
    that. Direction is a majority question; coverage is not.
    """
    return any(_in(ch, _ARABIC) for ch in text)


def has_any_rtl(text: str) -> bool:
    """Whether a single right-to-left letter appears at all.

    A weaker test than `is_rtl`, for the one case where a single letter
    matters: a paragraph holding any Arabic at all needs its direction set,
    because that is what decides where its punctuation and numbers land.
    """
    return any(_in(ch, _ARABIC + _HEBREW) for ch in text)


def _share(text: str, ranges: tuple) -> float:
    """The fraction of the letters in `text` that fall in these blocks.

    Letters only. Digits, spaces and punctuation are shared between the two
    scripts and counting them would make a mostly-Arabic line with a long
    number in it read as mixed.
    """
    letters = [ch for ch in text if ch.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for ch in letters if _in(ch, ranges)) / len(letters)


def _in(ch: str, ranges: tuple) -> bool:
    point = ord(ch)
    return any(low <= point <= high for low, high in ranges)
