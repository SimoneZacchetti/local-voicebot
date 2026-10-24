"""Prompt construction. The system prompt is byte-identical on every turn so the LLM
server can reuse its prompt (KV) cache; the reply language goes in the last message."""

from __future__ import annotations

from .langid import LANG_NAMES

SYSTEM_PROMPT = """You are a friendly voice assistant in a live spoken conversation.
Everything you write is converted to speech, so:
- Keep replies short and conversational: usually one to three sentences, unless the user explicitly asks for detail.
- Plain spoken text only: no markdown, lists, headings, emoji, tables, code or URLs.
- Write numbers, dates, units, acronyms and symbols the way they are spoken.
- Start directly with the answer. No filler openers, do not repeat the question.
- If the request is ambiguous, ask one short clarifying question.
- If your previous reply ends with "…", the user interrupted you there: do not resume it unless asked.
- Always reply in the language indicated at the end of the user's message."""

TOOLS_PROMPT = """

You can act on the user's home with tools (lights, plugs, climate, media players, lists, scripts, announcements).
- Use a home tool only when the user asks to control or check something in the house; otherwise answer directly.
- Every request to change something in the house MUST go through a tool call, every time, even if a similar
  request was handled before. Saying "fatto" without a tool call in this turn is a lie: never do it.
- When you call a tool, call it right away without writing any text first.
- To check states ("is the light on?", "what's the temperature?") use the live context tool.
- Use the device, area and room names the user says; prefer the area argument for rooms.
- Never claim an action was done unless the tool result confirms it. If a result reports an error or that nothing matched, say so briefly.
- Use the exact device and area names listed in the home context below (translate the user's words to them).
- Never ask for confirmation yourself: always call the tool. Safety checks are done by the system; if a result
  says needs_confirmation, the action was NOT done: ask one short yes/no question about it.
- After acting, confirm in a few words (e.g. "Fatto, luce della cucina accesa.")."""

INFO_PROMPT = """

You can look up the weather (get_weather) and the latest news headlines (get_news) with tools.
- Call them right away, without writing any text first.
- Every weather or news question needs a new call in this turn, even a follow-up ("e domani?", "and in London?"):
  earlier results in the conversation are cut short and may be old. Never answer weather or news from memory.
- Outdoor weather ("che tempo fa", "quanti gradi ci sono fuori") is get_weather; temperatures inside the house
  come from the home tools, when there are any.
- Weather: leave location empty when the user means where they are ("qui", "fuori", "here", or no place said);
  otherwise pass the place name. Answer only what was asked (sky, rounded temperature, rain), never every number.
  The result covers today and the next 6 days, each with its weekday: pick the day the user asked about.
- News: tell three headlines (fewer only if the result has fewer), one short sentence each, then offer to tell more.
- Never make up weather or news: if a tool fails, say you can't check right now."""

TOOLS_SECTION = ""  # set at startup (and when the home route changes): tool rules plus the Home Assistant index


def set_tool_context(tools) -> None:
    """Tool rules for the system prompt, from a ToolRegistry (home tools, weather and news)."""
    global TOOLS_SECTION
    home = tools.context.strip()
    TOOLS_SECTION = (
        (TOOLS_PROMPT if tools.home_enabled else "")
        + (INFO_PROMPT if tools.info_specs else "")
        + (f"\n\n# Home context (from Home Assistant)\n{home}" if tools.home_enabled and home else "")
    )


GREETINGS = {
    "it": "Ciao! Sono pronto, dimmi pure.",
    "en": "Hi! I'm ready, go ahead.",
    "fr": "Salut ! Je suis prêt, je t'écoute.",
    "de": "Hallo! Ich bin bereit, leg los.",
    "es": "¡Hola! Estoy listo, dime.",
    "pt": "Olá! Estou pronto, pode falar.",
    "nl": "Hallo! Ik ben er klaar voor, zeg het maar.",
}

# Short fixed replies for home actions (no LLM round trip needed).
DONE = {"it": "Fatto.", "en": "Done.", "fr": "C'est fait.", "de": "Erledigt.", "es": "Hecho.", "pt": "Feito.", "nl": "Gedaan."}
CANCELLED = {"it": "Va bene, annullato.", "en": "Okay, cancelled.", "fr": "D'accord, annulé.", "de": "Okay, abgebrochen.",
             "es": "Vale, cancelado.", "pt": "Certo, cancelado.", "nl": "Oké, geannuleerd."}
FAILED = {"it": "Non ci sono riuscito.", "en": "That didn't work.", "fr": "Je n'ai pas réussi.", "de": "Das hat nicht geklappt.",
          "es": "No lo he conseguido.", "pt": "Não consegui.", "nl": "Dat is niet gelukt."}

PARTIAL = {"it": "Non sono riuscito a fare tutto.", "en": "I couldn't do everything.", "fr": "Je n'ai pas tout fait.",
           "de": "Ich konnte nicht alles erledigen.", "es": "No he podido hacerlo todo.", "pt": "Não consegui fazer tudo.",
           "nl": "Het is niet allemaal gelukt."}


def language_tag(lang: str) -> str:
    # No date here or in the system prompt: with it Qwen answers weather and news from its own
    # imagination instead of calling the tools. The weather result names today and each weekday.
    return f"\n\n(Reply in {LANG_NAMES.get(lang, lang)}.)"


def system_prompt(with_tools: bool) -> str:
    return SYSTEM_PROMPT + TOOLS_SECTION if with_tools else SYSTEM_PROMPT


def build_messages(history: list[dict], lang: str, last_turns: int | None = None, with_tools: bool = False,
                   hint: str | None = None) -> list[dict]:
    """``history`` alternates user/assistant messages and ends with the current user message.
    ``hint``: a tool this message obviously needs (named in the message, never kept in history)."""
    msgs = history
    if last_turns is not None:
        # Keep at most ``last_turns`` exchanges (+ the current user message).
        starts = [i for i, m in enumerate(history) if m["role"] == "user"]
        msgs = history[starts[-(last_turns + 1)]:] if len(starts) > last_turns else history
    msgs = [dict(m) for m in msgs]
    if msgs and msgs[-1]["role"] == "user":
        msgs[-1]["content"] += (f"\n\n(Call {hint} for this.)" if hint else "") + language_tag(lang)
    return [{"role": "system", "content": system_prompt(with_tools)}, *msgs]
