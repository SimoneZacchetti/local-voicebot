"""Text hygiene: make LLM output speakable, and filter junk STT transcripts."""

from __future__ import annotations

import difflib
import re
import unicodedata

_URL = re.compile(r"https?://\S+|www\.\S+")
_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_CODE = re.compile(r"`+")
_BULLET = re.compile(r"^\s*(?:[-*+•]|\d+[.)])\s+", re.MULTILINE)
_HEADING = re.compile(r"^\s*#{1,6}\s*", re.MULTILINE)
# Paired emphasis only (**bold**, _italic_): keeps "3 * 4" and "my_file".
_EMPH = re.compile(r"(?<!\w)(\*{1,3}|_{1,3})(?=\S)(.+?)(?<=\S)\1(?!\w)")
_TIMES = re.compile(r"(?<=\d)\s*[*×]\s*(?=\d)")
_DEGREE = re.compile(r"\s?°\s?([CF])?\b|\s?°")
_QUOTE = re.compile(r"^\s*>\s?", re.MULTILINE)
_WS = re.compile(r"\s+")
_INVISIBLE = "️‍"  # emoji variation selector, zero-width joiner

DEGREE_WORDS = {"it": "gradi", "en": "degrees", "fr": "degrés", "de": "Grad", "es": "grados", "pt": "graus", "nl": "graden"}
TIMES_WORDS = {"it": "per", "en": "times", "fr": "fois", "de": "mal", "es": "por", "pt": "vezes", "nl": "keer"}


def _strip_symbols(text: str) -> str:
    # Drop emoji and other pictographic symbols (Unicode category "So"), keep letters/punctuation.
    return "".join(ch for ch in text if unicodedata.category(ch) != "So" and ch not in _INVISIBLE)


def _degrees(m: re.Match, lang: str) -> str:
    word = DEGREE_WORDS.get(lang, DEGREE_WORDS["en"])
    scale = m.group(1)
    return f" {word}" + (f" {'Celsius' if scale == 'C' else 'Fahrenheit'}" if scale else "")


def clean_for_tts(text: str, lang: str = "it") -> str:
    text = _MD_LINK.sub(r"\1", text)
    text = _URL.sub("", text)
    text = _CODE.sub("", text)
    text = _HEADING.sub("", text)
    text = _BULLET.sub("", text)
    text = _QUOTE.sub("", text)
    text = _EMPH.sub(r"\2", text)
    text = _TIMES.sub(f" {TIMES_WORDS.get(lang, 'x')} ", text)
    text = _DEGREE.sub(lambda m: _degrees(m, lang), text)
    text = _strip_symbols(text)
    text = _WS.sub(" ", text).strip()
    # Nothing speakable left (only punctuation)?
    if not any(ch.isalnum() for ch in text):
        return ""
    return text


# Known Whisper-style hallucinations produced on silence/noise.
_HALLUCINATIONS = (
    "amara.org",
    "sottotitoli creati dalla comunità",
    "sottotitoli a cura di",
    "grazie per la visione",
    "thanks for watching",
    "thank you for watching",
    "subtitles by",
    "please subscribe",
    "iscriviti al canale",
)


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).lower()
    text = re.sub(r"[^\w\s]", " ", text)
    return _WS.sub(" ", text).strip()


def is_hallucination(text: str) -> bool:
    n = text.lower()
    return any(h in n for h in _HALLUCINATIONS)


def is_echo(transcript: str, bot_sentences: list[str], threshold: float = 0.75) -> bool:
    """True when the transcript looks like the bot's own voice picked up by the mic.

    ``bot_sentences`` must be what the user could actually have heard. Matching is done
    on words, so a short real answer that reuses the bot's words ("il mare", "la
    montagna") is not mistaken for echo: up to 3 words it must match a whole sentence.
    """
    tw = normalize(transcript).split()
    sentences = [normalize(s).split() for s in bot_sentences if s.strip()]
    if not tw or not sentences:
        return False
    if len(tw) <= 3:
        joined = " ".join(tw)
        if any(difflib.SequenceMatcher(None, joined, " ".join(sw)).ratio() >= 0.9 for sw in sentences):
            return True
        # Exactly three words lifted verbatim from a sentence ("stavamo solo scambiandoci"): the
        # phone speaker leaking the reply. Two-word answers ("il mare") are never judged echo.
        return len(tw) == 3 and any(tw == sw[i:i + 3] for sw in sentences for i in range(len(sw) - 2))
    # Echo is usually a contiguous fragment, possibly spanning two sentences, with a few
    # words mis-transcribed: count how many transcript words align with the bot's words.
    bot_words = [w for sw in sentences for w in sw]
    sm = difflib.SequenceMatcher(None, tw, bot_words, autojunk=False)
    matched = sum(b.size for b in sm.get_matching_blocks())
    return matched / len(tw) >= threshold
