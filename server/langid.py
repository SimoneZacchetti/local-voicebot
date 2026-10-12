"""Language identification with a sticky session language and hysteresis.

Parakeet does not return a language label, so the language is detected from the
transcript text. Short or ambiguous utterances ("sì", "ok", "meeting") keep the
current conversation language.
"""

from __future__ import annotations

import re
from functools import cached_property

from lingua import IsoCode639_1, LanguageDetectorBuilder

from .config import cfg


class LangId:
    def __init__(self, langs: list[str] | None = None) -> None:
        self.langs = langs or cfg.langid_langs

    @cached_property
    def detector(self):
        # lingua lacks a few languages (e.g. Maltese): skip them.
        codes = [getattr(IsoCode639_1, c.upper()) for c in self.langs if hasattr(IsoCode639_1, c.upper())]
        return LanguageDetectorBuilder.from_iso_codes_639_1(*codes).with_preloaded_language_models().build()

    def warmup(self) -> None:
        self.detector.compute_language_confidence_values("ciao come stai oggi")

    def scores(self, text: str) -> dict[str, float]:
        return {
            r.language.iso_code_639_1.name.lower(): r.value
            for r in self.detector.compute_language_confidence_values(text)
        }

    def requested(self, text: str) -> str | None:
        """Language explicitly asked for ("parliamo in inglese", "speak French", "auf Deutsch").

        Not a request: a question about it ("perché mi parli in inglese?") or a negation
        ("non parlarmi in inglese"), judged on the few words before the verb in the same clause.
        """
        low = text.lower()
        m = _REQUEST.search(low)
        if not m:
            return None
        clause = re.split(r"[,.;:!?]", low[: m.start()])[-1].split()[-3:]
        if any(w in _NOT_A_REQUEST for w in clause):
            return None
        return _NAME_TO_LANG[m.group("name")]

    def foreign(self, text: str, sticky: str, max_sticky: float = 0.1, ratio: float = 2.5) -> bool:
        """``text`` is confidently in another language than ``sticky``, even when short.

        While the bot talks, that is almost always its own voice coming back through the
        speaker, mis-transcribed ("String.", "And look at it." during an Italian reply).
        """
        words = set(re.findall(r"\w+", text.lower()))
        if not words or words & _BARGE_WORDS or self.requested(text):
            return False
        scores = self.scores(text)
        if not scores:
            return False
        # Confidence is spread over ~25 languages, so short text never scores high: judge it
        # relatively. "String." -> it 0.05, sv 0.21; "va bene" -> it 0.08, sl 0.09 (kept).
        top, conf = max(scores.items(), key=lambda kv: kv[1])
        mine = scores.get(sticky, 0.0)
        return top != sticky and mine < max_sticky and conf >= ratio * mine

    def resolve(self, text: str, sticky: str, stt_lang: str | None = None) -> str:
        words = len(text.split())
        if words < cfg.langid_min_words:
            return sticky

        # Whisper/Groq give an acoustic language label; trust it for longer utterances.
        if stt_lang and stt_lang in self.langs:
            return stt_lang

        scores = self.scores(text)
        if not scores:
            return sticky
        top_lang, top_conf = max(scores.items(), key=lambda kv: kv[1])
        if top_lang == sticky:
            return sticky
        margin = top_conf - scores.get(sticky, 0.0)
        if top_conf >= cfg.langid_min_conf and margin >= cfg.langid_min_margin:
            return top_lang
        return sticky


# Full names in the target language, used in the LLM instruction.
LANG_NAMES = {
    "it": "italiano", "en": "English", "fr": "français", "de": "Deutsch", "es": "español",
    "pt": "português", "nl": "Nederlands", "pl": "polski", "ru": "русский", "uk": "українська",
    "cs": "čeština", "sk": "slovenčina", "sl": "slovenščina", "hr": "hrvatski", "bg": "български",
    "ro": "română", "hu": "magyar", "el": "ελληνικά", "sv": "svenska", "da": "dansk",
    "fi": "suomi", "et": "eesti", "lv": "latviešu", "lt": "lietuvių", "mt": "Malti",
}

# BCP-47 tags for the browser speechSynthesis fallback.
BCP47 = {
    "it": "it-IT", "en": "en-US", "fr": "fr-FR", "de": "de-DE", "es": "es-ES", "pt": "pt-PT",
    "nl": "nl-NL", "pl": "pl-PL", "ru": "ru-RU", "uk": "uk-UA", "cs": "cs-CZ", "sk": "sk-SK",
    "sl": "sl-SI", "hr": "hr-HR", "bg": "bg-BG", "ro": "ro-RO", "hu": "hu-HU", "el": "el-GR",
    "sv": "sv-SE", "da": "da-DK", "fi": "fi-FI", "et": "et-EE", "lv": "lv-LV", "lt": "lt-LT",
    "mt": "mt-MT",
}

# Groq/Whisper may return full English names instead of ISO codes.
WHISPER_NAME_TO_ISO = {
    "italian": "it", "english": "en", "french": "fr", "german": "de", "spanish": "es",
    "portuguese": "pt", "dutch": "nl", "polish": "pl", "russian": "ru", "ukrainian": "uk",
    "czech": "cs", "slovak": "sk", "slovenian": "sl", "croatian": "hr", "bulgarian": "bg",
    "romanian": "ro", "hungarian": "hu", "greek": "el", "swedish": "sv", "danish": "da",
    "finnish": "fi", "estonian": "et", "latvian": "lv", "lithuanian": "lt", "maltese": "mt",
}


def to_iso(lang: str | None) -> str | None:
    if not lang:
        return None
    lang = lang.strip().lower()
    if len(lang) == 2:
        return lang
    return WHISPER_NAME_TO_ISO.get(lang)


# Names of the languages with a local voice, as spoken in those same languages.
_LANGUAGE_WORDS = {
    "it": ["italiano", "italian", "italien", "italienisch", "italiaans"],
    "en": ["inglese", "english", "anglais", "englisch", "inglés", "ingles", "inglês", "engels"],
    "fr": ["francese", "french", "français", "francais", "französisch", "francés", "francês", "frans"],
    "de": ["tedesco", "german", "allemand", "deutsch", "alemán", "aleman", "alemão", "duits"],
    "es": ["spagnolo", "spanish", "espagnol", "spanisch", "español", "espanol", "espanhol", "spaans"],
    "pt": ["portoghese", "portuguese", "portugais", "portugiesisch", "portugués", "português", "portugees"],
    "nl": ["olandese", "dutch", "néerlandais", "niederländisch", "holandés", "holandês", "nederlands"],
}
_NAME_TO_LANG = {name: lang for lang, names in _LANGUAGE_WORDS.items() for name in names}
_NAMES = "|".join(sorted(map(re.escape, _NAME_TO_LANG), key=len, reverse=True))
# A speak/answer verb followed (within a few words) by the language name: "parliamo in
# inglese", "rispondimi in francese", "can you speak to me in English", "sprich Deutsch".
# A bare "in inglese" is not enough: "come si dice gatto in inglese?" is a translation question.
_VERBS = (
    "speak|talk|answer|reply|respond|switch to|"
    "parla|parli|parliamo|parlami|rispondi|rispondimi|rispondere|passiamo all?|"
    "parle|parlez|parlons|réponds|répondez|"
    "sprich|sprechen|sprichst|antworte|antworten|"
    "habla|hablas|hablemos|responde|respóndeme|"
    "fala|falar|falemos|responda|"
    "spreek|spreken|antwoord"
)
# Words before the verb that make it a question or a negation, not a request.
_NOT_A_REQUEST = {
    "perché", "perche", "why", "pourquoi", "warum", "porque", "qué", "waarom",
    "non", "not", "don't", "dont", "nicht", "ne", "no", "niet",
}
# Short interjections that interrupt the bot in any language: never treated as echo.
_BARGE_WORDS = {"stop", "ok", "okay", "basta", "aspetta", "wait", "no", "sì", "si", "yes", "ciao", "hey", "ehi"}
_REQUEST = re.compile(
    rf"\b(?:{_VERBS})(?:(?:\s+\S+){{0,3}}?\s+(?:in|en|auf|em|na|in het)\s+|\s+)(?P<name>{_NAMES})\b"
)
