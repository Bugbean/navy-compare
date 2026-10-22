"""Plainness: Flesch-Kincaid grade and sentence length (plan: grade 8 or below, under 20 words a
sentence). Logged on every script in the pilot, enforced only by the certification set."""
from __future__ import annotations

import re

_SENTENCE = re.compile(r"[^.!?]+[.!?]?")
_WORD = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?|\d[\d,.]*")


def _syllables(word: str) -> int:
    """A vowel-group count, which is what FK scoring tools use: good enough to rank prose, not a lexicon."""
    w = word.lower()
    if w[0].isdigit():
        return 1
    groups = re.findall(r"[aeiouy]+", w)
    n = len(groups)
    if w.endswith("e") and not w.endswith(("le", "ee")) and n > 1:
        n -= 1
    return max(1, n)


def sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE.findall(text) if _WORD.search(s)]


def max_sentence_words(text: str) -> int:
    return max((len(_WORD.findall(s)) for s in sentences(text)), default=0)


def fk_grade(text: str) -> float:
    """Flesch-Kincaid grade level: 0.39 (words/sentence) + 11.8 (syllables/word) - 15.59."""
    sents = sentences(text)
    words = [w for s in sents for w in _WORD.findall(s)]
    if not sents or not words:
        return 0.0
    syl = sum(_syllables(w) for w in words)
    return round(0.39 * len(words) / len(sents) + 11.8 * syl / len(words) - 15.59, 1)
