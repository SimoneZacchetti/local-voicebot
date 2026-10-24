"""Weather and news tools, from free web services that need no API key.

- Weather: Open-Meteo (forecast and place search). With no place named, the user's own position:
  the one the browser or phone sent, else WEATHER_LOCATION, else a lookup of the public IP.
- Places for a position: OpenStreetMap Nominatim (reverse geocoding, cached).
- News: one RSS feed per language (NEWS_FEED_<LANG>); news about a topic ("notizie di Madrid") from a
  Google News search in that language, which covers any place or subject (NEWS_SEARCH=0 turns it off).

Positions are rounded to 0.1° (about 10 km) before they leave this computer. Every answer is
compact JSON for the LLM, which turns it into a short spoken reply, plus a "card" for the
screen (forecast with icons, headlines with links and pictures) that the LLM never sees.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
import time
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date

import httpx

from .config import cfg

log = logging.getLogger("voicebot.info")

USER_AGENT = "local-voicebot/0.1 (+https://github.com/SimoneZacchetti/local-voicebot)"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"
IP_URLS = ("https://ipwho.is/", "https://ipapi.co/json/")
NEWS_SEARCH_URL = "https://news.google.com/rss/search"
# Google News edition (hl, gl, ceid) per voice language; other languages get the English one.
NEWS_EDITIONS = {
    "it": ("it", "IT", "IT:it"), "en": ("en-GB", "GB", "GB:en"), "fr": ("fr", "FR", "FR:fr"),
    "de": ("de", "DE", "DE:de"), "es": ("es", "ES", "ES:es"), "pt": ("pt-PT", "PT", "PT:pt-150"),
    "nl": ("nl", "NL", "NL:nl"),
}
MAX_FEED_BYTES = 2_000_000
CARD_NEWS_ITEMS = 5
MIN_NEWS_ITEMS = 3  # with a topic, what the answer tells (see prompts.INFO_PROMPT)
MEDIA = "{http://search.yahoo.com/mrss/}"
ATOM = "{http://www.w3.org/2005/Atom}"

WEATHER_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": (
            "Current weather, next hours and the forecast for today and the next 6 days. Use it for any "
            "question about weather, temperature, "
            "rain, wind or sunrise/sunset. Leave location empty when the user means where they are now "
            "(\"qui\", \"fuori\", \"here\", or no place said)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "location": {"type": "string", "description": "City or town name; empty for the user's position"},
            },
        },
    },
}

NEWS_TOOL = {
    "type": "function",
    "function": {
        "name": "get_news",
        "description": "Latest news headlines in the user's language. Use it when the user asks for news or "
                       "what is happening in the world.",
        "parameters": {
            "type": "object",
            "properties": {"topic": {"type": "string", "description": "Optional subject, e.g. 'sport', 'Ukraine'"}},
        },
    },
}

# WMO weather interpretation codes (Open-Meteo "weather_code").
WMO = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "freezing fog",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 56: "freezing drizzle", 57: "freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "light showers", 81: "showers", 82: "violent showers", 85: "snow showers", 86: "heavy snow showers",
    95: "thunderstorm", 96: "thunderstorm with hail", 99: "thunderstorm with heavy hail",
}

# Words meaning "where I am": the LLM sometimes passes them instead of leaving location empty.
HERE_WORDS = {"", "qui", "qua", "here", "ici", "hier", "aqui", "aca", "current location", "my location",
              "posizione attuale", "la mia posizione", "dove sono", "user location", "current"}


# Questions that need a tool, in the 7 voice languages. After a few tool turns Qwen tends to answer a
# follow-up ("and in London?") from memory, so these get an explicit hint (see prompts.build_messages).
# Phrases, not single words: "tempo fa" is also "ago", "tiempo" is also "time".
WEATHER_RE = re.compile(
    r"\b(meteo|che tempo|tempo (far[aà]|c'[eè])|com'?[eè] il tempo|piov\w*|pioggia|nevic\w*|neve|ombrell\w*|previsioni"
    r"|weather|forecast|rain\w*|snow\w*|umbrella|m[eé]t[eé]o|pluie|pleut|neige|wetter|regen\w*|schnee"
    r"|qu[eé] tiempo|tiempo (hace|har[aá])|lluv\w*|llueve|nieve|previs[aã]o|chuv\w*|chov\w*|weer(bericht)?|sneeuw)\b"
    r"|\b(gradi|temperatur\w*|degrees|grad|degr[eé]s|graus|graden)\b.*\b(fuori|outside|out there|dehors|drau[sß]en"
    r"|fuera|l[aá] fora|buiten)\b", re.IGNORECASE)
NEWS_RE = re.compile(r"\b(notizi[ae]|news|nouvelles|actualit[eé]s?|nachrichten|noticias?|not[ií]cias?|nieuws"
                     r"|telegiornale|headlines)\b", re.IGNORECASE)


def route(text: str | None) -> str | None:
    """The info tool a question obviously needs, if any."""
    if not text:
        return None
    if WEATHER_RE.search(text):
        return "get_weather"
    if NEWS_RE.search(text):
        return "get_news"
    return None


@dataclass
class Place:
    lat: float
    lon: float
    name: str | None = None
    source: str = "named"  # named | device | config | ip

    @property
    def key(self) -> tuple[float, float]:
        # 0.1° (~11 km north-south, ~8 km east-west in Italy): the only precision that leaves this computer.
        # Enough for the weather and the town name, not for finding a house.
        return round(self.lat, 1), round(self.lon, 1)


def fold(text: str) -> str:
    """Lowercase, no accents: for matching words said by voice."""
    text = unicodedata.normalize("NFKD", text.casefold())
    return "".join(c for c in text if not unicodedata.combining(c)).strip()


def sky(code) -> str:
    return WMO.get(int(code), "unknown") if code is not None else "unknown"


def _r(x) -> int | None:
    return None if x is None else round(x)


def parse_coords(text: str) -> tuple[float, float] | None:
    m = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*", text)
    if not m:
        return None
    lat, lon = float(m[1]), float(m[2])
    return (lat, lon) if valid_coords(lat, lon) else None


def valid_coords(lat, lon) -> bool:
    try:
        return -90 <= float(lat) <= 90 and -180 <= float(lon) <= 180
    except (TypeError, ValueError):
        return False


class Info:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        # The whole tool budget for connecting too: some networks (firewalls dropping SYNs) take seconds.
        # Connections are kept open a while, so a follow-up question skips that.
        self.client = client or httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT}, follow_redirects=True,
            timeout=httpx.Timeout(max(1.0, cfg.tool_timeout_s - 1)),
            limits=httpx.Limits(keepalive_expiry=120),
        )
        self._cache: dict[tuple, tuple[float, object]] = {}
        self.specs = ([WEATHER_TOOL] if "weather" in cfg.info_tools else []) + \
                     ([NEWS_TOOL] if "news" in cfg.info_tools else [])
        self.names = {s["function"]["name"] for s in self.specs}

    # -- plumbing -----------------------------------------------------------------------

    async def _cached(self, key: tuple, ttl: float, fetch):
        hit = self._cache.get(key)
        if hit is not None and hit[0] > time.monotonic():
            return hit[1]
        value = await fetch()
        if len(self._cache) > 256:
            self._cache = {k: v for k, v in self._cache.items() if v[0] > time.monotonic()}
        self._cache[key] = (time.monotonic() + ttl, value)
        return value

    async def _json(self, url: str, params: dict | None = None):
        r = await self.client.get(url, params=params)
        r.raise_for_status()
        return r.json()

    async def execute(self, name: str, args: dict, lang: str, position: tuple[float, float] | None = None) -> dict:
        try:
            if name == "get_weather":
                return await self.weather(args, lang, position)
            if name == "get_news":
                return await self.news(args, lang)
            return {"ok": False, "error": f"unknown tool {name}"}
        except httpx.HTTPError as e:
            log.warning("%s failed: %r", name, e)
            return {"ok": False, "error": f"service unreachable ({type(e).__name__}): say you can't check right now"}

    async def warmup(self) -> None:
        """Look up the fallback position now, not during the first question."""
        if "weather" in self.names and not cfg.weather_location:
            try:
                await self.ip_place()
            except httpx.HTTPError as e:
                log.warning("IP location lookup failed: %r", e)

    # -- places ---------------------------------------------------------------------------

    async def place(self, name: str, lang: str) -> Place | None:
        """A place said by the user ("Roma", "Parigi, Francia")."""
        async def fetch():
            for query in dict.fromkeys([name.strip(), name.split(",")[0].strip()]):
                res = (await self._json(GEOCODE_URL, {"name": query, "count": 1, "language": lang})).get("results")
                if res:
                    r = res[0]
                    label = ", ".join(x for x in (r.get("name"), r.get("admin1"), r.get("country")) if x)
                    return Place(r["latitude"], r["longitude"], label)
            return None
        return await self._cached(("place", fold(name), lang), 7 * 86400, fetch)

    async def place_name(self, lat: float, lon: float, lang: str) -> str | None:
        """Town name of a position (best effort: the weather does not wait long for it)."""
        p = Place(lat, lon)

        async def fetch():
            j = await self._json(REVERSE_URL, {"lat": p.key[0], "lon": p.key[1], "format": "jsonv2", "zoom": 10,
                                               "accept-language": lang})
            a = j.get("address") or {}
            return a.get("city") or a.get("town") or a.get("village") or a.get("municipality") or j.get("name")
        try:
            return await self._cached(("rev", *p.key, lang), 86400, fetch)
        except (httpx.HTTPError, ValueError) as e:
            log.info("reverse geocoding failed: %r", e)
            return None

    async def ip_place(self) -> Place | None:
        """Approximate position of this computer's public IP (a VPN or corporate proxy moves it)."""
        async def fetch():
            for url in IP_URLS:
                try:
                    j = await self._json(url)
                except (httpx.HTTPError, ValueError) as e:
                    log.info("IP location via %s failed: %r", url, e)
                    continue
                if j.get("success", True) and valid_coords(j.get("latitude"), j.get("longitude")):
                    return Place(float(j["latitude"]), float(j["longitude"]), j.get("city"), "ip")
            return None
        return await self._cached(("ip",), 3600, fetch)

    async def here(self, lang: str, position: tuple[float, float] | None) -> Place | None:
        """Where the user is: device position, else WEATHER_LOCATION, else the public IP."""
        if position is not None:
            return Place(*position, source="device")
        if cfg.weather_location:
            if coords := parse_coords(cfg.weather_location):
                return Place(*coords, source="config")
            p = await self.place(cfg.weather_location, lang)
            if p is not None:
                return Place(p.lat, p.lon, p.name, "config")
        return await self.ip_place()

    # -- weather --------------------------------------------------------------------------

    async def weather(self, args: dict, lang: str, position: tuple[float, float] | None = None) -> dict:
        where = str(args.get("location") or "").strip()
        if fold(where) in HERE_WORDS:
            place = await self.here(lang, position)
            if place is None:
                return {"ok": False, "error": "the user's position is unknown: ask which city"}
        else:
            place = await self.place(where, lang)
            if place is None:
                return {"ok": False, "error": f"place not found: {where}. Ask the user to repeat the city name"}
        name_task = None
        if place.name is None:
            name_task = asyncio.ensure_future(self.place_name(place.lat, place.lon, lang))
        try:
            raw = await self._cached(("wx", *place.key), 600, lambda: self._forecast(place))
            if name_task is not None:
                try:
                    place.name = await asyncio.wait_for(name_task, 1.5)
                except TimeoutError:
                    pass
        finally:
            if name_task is not None and not name_task.done():
                name_task.cancel()
        out = {"ok": True, "place": place.name or "the user's current position"}
        if place.source == "ip":
            out["note"] = "position estimated from the internet connection: may be wrong, name the place"
        card = {"kind": "weather", "place": place.name, "approximate": place.source == "ip", **weather_card(raw)}
        return {**out, **summarize(raw), "card": card}

    async def _forecast(self, place: Place) -> dict:
        return await self._json(FORECAST_URL, {
            "latitude": place.key[0], "longitude": place.key[1], "timezone": "auto",
            "forecast_days": 7,
            "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m,precipitation,is_day",
            "hourly": "temperature_2m,precipitation_probability,weather_code,is_day",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,"
                     "precipitation_sum,sunrise,sunset",
        })

    # -- news ---------------------------------------------------------------------------

    async def news(self, args: dict, lang: str) -> dict:
        topic = str(args.get("topic") or "").strip()
        if topic and cfg.news_search and (found := await self.search_news(topic, lang)):
            return found
        url = cfg.news_feeds.get(lang) or cfg.news_feeds.get("en")
        if not url:
            return {"ok": False, "error": "no news source configured for this language"}

        async def fetch():
            r = await self.client.get(url)
            r.raise_for_status()
            if len(r.content) > MAX_FEED_BYTES:
                raise ValueError("feed too large")
            return parse_feed(r.content)
        try:
            source, items = await self._cached(("news", url), 900, fetch)
        except (ET.ParseError, ValueError) as e:
            log.warning("news feed %s unreadable: %r", url, e)
            return {"ok": False, "error": "the news source answered with something unreadable"}
        out: dict = {"ok": True, "source": source}
        if topic:
            words = [w for w in re.findall(r"\w+", fold(topic)) if len(w) >= 3]
            hits = [i for i in items if any(w in fold(f"{i['title']} {i['summary']}") for w in words)]
            if not hits:
                out["note"] = f"no headline about {topic} right now: these are the top headlines"
            elif len(hits) < MIN_NEWS_ITEMS:
                # Always three to tell: fill with the top headlines, and say which ones are on the topic.
                out["note"] = (f"only the first {len(hits)} headline(s) are about {topic}: "
                               "the others are top headlines, say so")
                hits += [i for i in items if i not in hits][: MIN_NEWS_ITEMS - len(hits)]
            items = hits or items
        out["headlines"] = [{"title": i["title"], "summary": i["summary"]} for i in items[: cfg.news_max_items]]
        out["card"] = {"kind": "news", "source": source, "items": items[:CARD_NEWS_ITEMS]}
        return out

    async def search_news(self, topic: str, lang: str) -> dict | None:
        """Latest news about a topic from Google News, or None (the feed of the language then filters)."""
        hl, gl, ceid = NEWS_EDITIONS.get(lang, NEWS_EDITIONS["en"])

        async def fetch():
            r = await self.client.get(NEWS_SEARCH_URL, params={"q": topic, "hl": hl, "gl": gl, "ceid": ceid})
            r.raise_for_status()
            if len(r.content) > MAX_FEED_BYTES:
                raise ValueError("feed too large")
            return parse_feed(r.content)[1]
        try:
            items = await self._cached(("news-search", fold(topic), lang), 900, fetch)
        except (httpx.HTTPError, ET.ParseError, ValueError) as e:
            log.info("news search for %r failed: %r", topic, e)
            return None
        if not items:
            return None
        # Titles end with " - <newspaper>"; the description only repeats them.
        items = [{**i, **dict(zip(("title", "summary"), split_outlet(i["title"])))} for i in items]
        return {"ok": True, "source": "Google News",
                "headlines": [{"title": i["title"], "newspaper": i["summary"]} for i in items[: cfg.news_max_items]],
                "card": {"kind": "news", "source": f"Google News · {topic}", "items": items[:CARD_NEWS_ITEMS]}}


def summarize(j: dict) -> dict:
    """Open-Meteo forecast -> the few numbers worth saying. Every day, named by weekday: the LLM
    picks "sabato" from it (it gets no date of its own, see prompts.language_tag)."""
    cur = j.get("current") or {}
    now_iso = str(cur.get("time", ""))
    out: dict = {"local_time": now_iso[11:16], "now": {
        "sky": sky(cur.get("weather_code")), "temp_c": _r(cur.get("temperature_2m")),
        "feels_like_c": _r(cur.get("apparent_temperature")), "wind_kmh": _r(cur.get("wind_speed_10m")),
        "precipitation_mm": cur.get("precipitation"),
    }}
    h = j.get("hourly") or {}
    times = h.get("time") or []
    start = next((i for i, t in enumerate(times) if t[:13] >= now_iso[:13]), len(times))
    out["next_hours"] = [{
        "time": times[i][11:16], "sky": sky(h["weather_code"][i]), "temp_c": _r(h["temperature_2m"][i]),
        "rain_chance_pct": h["precipitation_probability"][i],
    } for i in range(start + 3, min(start + 25, len(times)), 3)]  # every 3 hours, next 24 hours
    d = j.get("daily") or {}
    out["days"] = [{
        "date": t, "weekday": date.fromisoformat(t).strftime("%A"), "sky": sky(d["weather_code"][i]),
        "min_c": _r(d["temperature_2m_min"][i]), "max_c": _r(d["temperature_2m_max"][i]),
        "rain_chance_pct": d["precipitation_probability_max"][i], "rain_mm": d["precipitation_sum"][i],
        "sunrise": str(d["sunrise"][i])[11:16], "sunset": str(d["sunset"][i])[11:16],
    } for i, t in enumerate(d.get("time") or [])]
    if out["days"]:
        out["today"] = f'{out["days"][0]["weekday"]} {out["days"][0]["date"]}'
    return out


def weather_card(j: dict) -> dict:
    """Open-Meteo forecast -> what the screen shows: weather codes for the icons, every hour of the next day."""
    cur = j.get("current") or {}
    now_iso = str(cur.get("time", ""))
    h = j.get("hourly") or {}
    times = h.get("time") or []
    start = next((i for i, t in enumerate(times) if t[:13] >= now_iso[:13]), len(times))
    d = j.get("daily") or {}
    return {
        "now": {"temp": _r(cur.get("temperature_2m")), "feels": _r(cur.get("apparent_temperature")),
                "code": cur.get("weather_code"), "sky": sky(cur.get("weather_code")), "day": cur.get("is_day", 1),
                "wind": _r(cur.get("wind_speed_10m")), "precip": cur.get("precipitation")},
        "hours": [{"time": times[i][11:16], "temp": _r(h["temperature_2m"][i]), "code": h["weather_code"][i],
                   "day": (h.get("is_day") or [1] * len(times))[i], "rain": h["precipitation_probability"][i]}
                  for i in range(start, min(start + 24, len(times)))],
        "days": [{"date": t, "code": d["weather_code"][i], "sky": sky(d["weather_code"][i]),
                  "min": _r(d["temperature_2m_min"][i]), "max": _r(d["temperature_2m_max"][i]),
                  "rain": d["precipitation_probability_max"][i]} for i, t in enumerate(d.get("time") or [])],
    }


# -- feeds ---------------------------------------------------------------------------------

_TAG = re.compile(r"<[^>]+>")


def clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", html.unescape(_TAG.sub(" ", text or ""))).strip()


def split_outlet(title: str) -> tuple[str, str]:
    """Google News title "Headline - Newspaper" -> ("Headline", "Newspaper")."""
    head, sep, outlet = title.rpartition(" - ")
    return (head, outlet) if sep and head else (title, "")


def safe_url(url: str | None) -> str | None:
    """Only plain web links reach the page (no javascript:, data: ...)."""
    url = html.unescape((url or "").strip())
    return url if re.match(r"https?://[^\s\"'<>]+$", url) and len(url) <= 2000 else None


def _image(item: ET.Element) -> str | None:
    """Picture of a feed item: media:thumbnail, media:content or an image enclosure."""
    for el in (*item.iter(f"{MEDIA}thumbnail"), *item.iter(f"{MEDIA}content"), *item.iter("enclosure")):
        kind = el.get("type") or el.get("medium") or "image"
        if kind.startswith("image") and (url := safe_url(el.get("url"))):
            return url
    return None


def parse_feed(xml: bytes | str) -> tuple[str, list[dict]]:
    """RSS 2.0 (or Atom) -> (source title, [{title, summary, link, image}])."""
    root = ET.fromstring(xml)
    channel = root.find("channel")
    if channel is not None:
        source = clean(channel.findtext("title"))
        items = [(i.findtext("title"), i.findtext("description"), i.findtext("link"), _image(i))
                 for i in channel.iter("item")]
    else:
        source = clean(root.findtext(f"{ATOM}title"))
        items = [(e.findtext(f"{ATOM}title"), e.findtext(f"{ATOM}summary"),
                  getattr(e.find(f"{ATOM}link"), "attrib", {}).get("href"), _image(e))
                 for e in root.iter(f"{ATOM}entry")]
    out = []
    for title, summary, link, image in items:
        title = clean(title)
        if title:
            summary = clean(summary)
            out.append({"title": title, "summary": summary[:200] + ("…" if len(summary) > 200 else ""),
                        "link": safe_url(link), "image": image})
    return source, out
