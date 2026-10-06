"""Streaming sentence chunker: turns LLM token deltas into speakable chunks.

The first chunk of a reply is cut eagerly (at a comma or after ~60 chars) so TTS can
start while the LLM is still generating; later chunks are whole sentences.
"""

from __future__ import annotations

import re

# Lower-cased abbreviations (without the final dot) after which a "." is not a boundary.
ABBREVIATIONS = {
    # Italian
    "dott", "dott.ssa", "sig", "sig.ra", "sigg", "prof", "prof.ssa", "ing", "avv", "arch",
    "ecc", "p.es", "cfr", "dr", "s.p.a", "s.r.l",
    # English
    "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "vs", "etc", "e.g", "i.e", "approx",
    "inc", "ltd", "fig", "jan", "feb", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec",
}
# Also ordinary words ("no", "es", "mar"): abbreviations only when a number follows ("n. 5").
NUMERIC_ABBREVIATIONS = {"n", "nr", "no", "es", "pag", "tel", "art", "cap", "vol", "ca", "st", "ss", "co", "mt", "mar"}

_STRONG = ".!?…;:"
_SOFT = ",—–"


class Chunker:
    def __init__(
        self,
        min_chars: int = 20,
        first_min_words: int = 5,
        first_max_chars: int = 60,
        min_words: int = 3,
        hard_max_chars: int = 200,
    ) -> None:
        self.buf = ""
        self.emitted = 0
        self.min_chars = min_chars
        self.first_min_words = first_min_words
        self.first_max_chars = first_max_chars
        self.min_words = min_words
        self.hard_max_chars = hard_max_chars

    # -- public API -------------------------------------------------------------------

    def feed(self, delta: str) -> list[str]:
        self.buf += delta
        out: list[str] = []
        while True:
            cut = self._find_cut()
            if cut is None:
                break
            piece = self.buf[:cut].strip()
            self.buf = self.buf[cut:]
            if piece:
                out.append(piece)
                self.emitted += 1
        return out

    def flush(self) -> list[str]:
        piece = self.buf.strip()
        self.buf = ""
        if piece:
            self.emitted += 1
            return [piece]
        return []

    # -- internals --------------------------------------------------------------------

    def _find_cut(self) -> int | None:
        buf = self.buf
        first = self.emitted == 0

        # Paragraph/newline boundary (list items often have no final punctuation).
        nl = buf.find("\n")
        while nl != -1:
            if self._words(buf[:nl]) >= self.min_words or (not first and self._words(buf[:nl]) >= 1):
                return nl + 1
            nl = buf.find("\n", nl + 1)

        for i, ch in enumerate(buf):
            if ch in _STRONG:
                end = self._boundary_end(buf, i)
                if end is None:
                    continue
                piece = buf[:end].strip()
                if len(piece) < self.min_chars or self._words(piece) < self.min_words:
                    # Too short: keep accumulating; merge with the next sentence.
                    continue
                return end
            if first and ch in _SOFT:
                if i + 1 < len(buf) and buf[i + 1].isspace() and self._words(buf[:i]) >= self.first_min_words:
                    return i + 1

        # Eager first chunk / hard limit: cut at the last whitespace.
        limit = self.first_max_chars if first else self.hard_max_chars
        if len(buf) > limit:
            ws = buf.rfind(" ", 0, limit + 1)
            if ws > 0 and self._words(buf[:ws]) >= self.min_words:
                return ws + 1
        return None

    def _boundary_end(self, buf: str, i: int) -> int | None:
        """Return the cut index if the strong punctuation at ``i`` ends a sentence."""
        j = i + 1
        # Consume runs like "?!" or "..." and closing quotes/brackets.
        while j < len(buf) and (buf[j] in _STRONG or buf[j] in "\"'»”’)]"):
            j += 1
        if j >= len(buf):
            return None  # need lookahead: maybe more punctuation/digits follow
        if not buf[j].isspace():
            return None  # "3.5", "10:30", "e.g." mid-token, "...ciao"
        ch = buf[i]
        if ch == "." and j == i + 1:
            word = re.search(r"(?<![\w'’])([\w.]+)$", buf[:i])
            if word:
                raw = word.group(1)
                w = raw.lower()
                if w in ABBREVIATIONS:
                    return None
                if w in NUMERIC_ABBREVIATIONS and buf[j:].lstrip()[:1].isdigit():
                    return None  # "n. 5", "pag. 12"
                if len(raw) == 1 and raw.isupper():
                    return None  # single initial "J. Smith" (not elided "c'è.")
        if ch == ":" and j < len(buf) and buf[j:].lstrip()[:1].isdigit():
            return None
        return j

    @staticmethod
    def _words(s: str) -> int:
        return len(s.split())
