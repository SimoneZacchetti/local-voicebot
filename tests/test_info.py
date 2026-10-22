"""Weather and news tools: web services faked with httpx.MockTransport, registry and prompt wiring."""

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from server import main, prompts
from server.config import cfg
from server.info import Info, parse_feed, summarize
from server.langid import LangId
from server.prompts import build_messages, set_tool_context, system_prompt
from server.session import Services, Session
from server.tools import ToolCall, ToolRegistry

from .test_tools import FakeHome
from .test_ws_protocol import FakeStt, FakeTts, recv, recv_until

FORECAST = {
    "current": {"time": "2026-10-22T10:15", "temperature_2m": 13.4, "apparent_temperature": 12.6,
                "weather_code": 61, "wind_speed_10m": 7.2, "precipitation": 0.3},
    "hourly": {
        "time": [f"2026-10-{22 + h // 24}T{h % 24:02d}:00" for h in range(48)],
        "temperature_2m": [10 + h % 24 / 4 for h in range(48)],
        "precipitation_probability": [h % 100 for h in range(48)],
        "weather_code": [3] * 48,
    },
    "daily": {
        "time": ["2026-10-22", "2026-10-23", "2026-10-24"],
        "weather_code": [61, 0, 95], "temperature_2m_max": [15.6, 18.2, 14.0], "temperature_2m_min": [9.4, 8.1, 7.7],
        "precipitation_probability_max": [80, 5, 90], "precipitation_sum": [3.2, 0.0, 12.5],
        "sunrise": ["2026-10-22T07:41"] * 3, "sunset": ["2026-10-22T18:21"] * 3,
    },
}

RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Primo piano ANSA - ANSA.it</title>
<item><title><![CDATA[Ora solare, i consigli utili]]></title>
  <description><![CDATA[ Dormire <b>meno</b> &egrave; diventata norma ]]></description></item>
<item><title>Serie A, l'Inter vince il derby</title><description>Sport: 2 a 1 a San Siro</description></item>
<item><title></title><description>senza titolo</description></item>
</channel></rss>"""

ATOM = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>Feed</title>
<entry><title>Headline one</title><summary>Short &amp; sweet</summary></entry></feed>"""


class Web:
    """Fake web services. Records every request; ``down`` makes them fail."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.down = False

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        if self.down:
            raise httpx.ConnectError("offline", request=req)
        host, q = req.url.host, req.url.params
        if host == "geocoding-api.open-meteo.com":
            if q["name"].lower() == "roma":
                return httpx.Response(200, json={"results": [{"name": "Roma", "latitude": 41.89193, "longitude": 12.51133,
                                                              "admin1": "Lazio", "country": "Italia"}]})
            return httpx.Response(200, json={})
        if host == "api.open-meteo.com":
            return httpx.Response(200, json=FORECAST)
        if host == "nominatim.openstreetmap.org":
            return httpx.Response(200, json={"name": "Milano", "address": {"city": "Milano"}})
        if host == "ipwho.is":
            return httpx.Response(200, json={"success": True, "latitude": 40.4165, "longitude": -3.7026, "city": "Madrid"})
        if host == "www.ansa.it":
            return httpx.Response(200, content=RSS)
        if host == "feeds.bbci.co.uk":
            return httpx.Response(200, content=ATOM)
        return httpx.Response(404)

    def hosts(self) -> list[str]:
        return [r.url.host for r in self.requests]


@pytest.fixture
def web(monkeypatch):
    monkeypatch.setattr(cfg, "info_tools", ["weather", "news"])
    monkeypatch.setattr(cfg, "weather_location", "")
    monkeypatch.setattr(prompts, "TOOLS_SECTION", prompts.TOOLS_SECTION)  # restored after set_tool_context
    w = Web()
    w.info = Info(httpx.AsyncClient(transport=httpx.MockTransport(w.handler)))
    return w


# -- weather ------------------------------------------------------------------------------

def test_summarize_keeps_what_is_worth_saying():
    out = summarize(FORECAST)
    assert out["local_time"] == "10:15"
    assert out["now"] == {"sky": "light rain", "temp_c": 13, "feels_like_c": 13, "wind_kmh": 7, "precipitation_mm": 0.3}
    assert [d["weekday"] for d in out["days"]] == ["Thursday", "Friday", "Saturday"]
    assert out["today"] == "Thursday 2026-10-22"
    assert out["days"][0] | {} == {"date": "2026-10-22", "weekday": "Thursday", "sky": "light rain", "min_c": 9, "max_c": 16,
                                   "rain_chance_pct": 80, "rain_mm": 3.2, "sunrise": "07:41", "sunset": "18:21"}
    # Every 3 hours from now (10:00) for a day, crossing midnight.
    assert [h["time"] for h in out["next_hours"]] == ["13:00", "16:00", "19:00", "22:00", "01:00", "04:00", "07:00", "10:00"]


async def test_weather_for_a_named_place(web):
    res = await web.info.weather({"location": "Roma"}, "it")
    assert res["ok"] and res["place"] == "Roma, Lazio, Italia"
    assert len(res["days"]) == 3 and res["days"][2]["sky"] == "thunderstorm"
    fc = next(r for r in web.requests if r.url.host == "api.open-meteo.com")
    assert (fc.url.params["latitude"], fc.url.params["longitude"]) == ("41.9", "12.5")  # rounded to 0.1°, ~10 km
    assert "note" not in res


async def test_weather_here_uses_the_device_position(web):
    res = await web.info.weather({"location": "qui"}, "it", position=(45.464211, 9.189982))
    assert res["ok"] and res["place"] == "Milano"
    assert "ipwho.is" not in web.hosts()
    for r in web.requests:  # the exact position never leaves this computer
        assert "45.464211" not in str(r.url) and "9.189982" not in str(r.url)
        if "lat" in r.url.params or "latitude" in r.url.params:
            assert {r.url.params.get("lat", r.url.params.get("latitude")),
                    r.url.params.get("lon", r.url.params.get("longitude"))} == {"45.5", "9.2"}


async def test_weather_without_a_position_falls_back_to_the_ip(web):
    res = await web.info.weather({}, "it")
    assert res["ok"] and res["place"] == "Madrid"
    assert "may be wrong" in res["note"]


async def test_weather_location_from_config(web, monkeypatch):
    monkeypatch.setattr(cfg, "weather_location", "Roma")
    res = await web.info.weather({"location": ""}, "it")
    assert res["place"] == "Roma, Lazio, Italia" and "note" not in res
    monkeypatch.setattr(cfg, "weather_location", "45.46, 9.19")
    res = await web.info.weather({}, "it")
    assert res["place"] == "Milano"  # reverse geocoded
    assert "ipwho.is" not in web.hosts()


async def test_weather_is_cached(web):
    for _ in range(3):
        await web.info.weather({"location": "Roma"}, "it")
    assert web.hosts().count("api.open-meteo.com") == 1
    assert web.hosts().count("geocoding-api.open-meteo.com") == 1


async def test_weather_errors_become_results(web):
    res = await web.info.weather({"location": "Xyzzyville", "days": "lots"}, "it")  # unknown args are ignored
    assert not res["ok"] and "Xyzzyville" in res["error"]
    web.down = True
    res = await web.info.execute("get_weather", {"location": "Parigi"}, "it")
    assert not res["ok"] and "can't check" in res["error"]


# -- news ---------------------------------------------------------------------------------

def test_parse_feed_cleans_cdata_and_html():
    source, items = parse_feed(RSS)
    assert source == "Primo piano ANSA - ANSA.it"
    assert items[0] == {"title": "Ora solare, i consigli utili", "summary": "Dormire meno è diventata norma"}
    assert len(items) == 2  # no empty titles
    assert parse_feed(ATOM) == ("Feed", [{"title": "Headline one", "summary": "Short & sweet"}])


async def test_news_in_the_user_language_with_topic(web):
    res = await web.info.news({}, "it")
    assert res["ok"] and res["source"].startswith("Primo piano ANSA") and len(res["headlines"]) == 2
    res = await web.info.news({"topic": "Sport"}, "it")
    assert [h["title"] for h in res["headlines"]] == ["Serie A, l'Inter vince il derby"] and "note" not in res
    res = await web.info.news({"topic": "Marte"}, "it")
    assert "no headline about Marte" in res["note"] and len(res["headlines"]) == 2
    assert web.hosts().count("www.ansa.it") == 1  # cached
    res = await web.info.news({}, "pl")  # no Polish source: the English one
    assert res["headlines"][0]["title"] == "Headline one"


async def test_unreadable_feed(web):
    web.handler = lambda req: httpx.Response(200, content=b"<html>not a feed")
    web.info.client = httpx.AsyncClient(transport=httpx.MockTransport(web.handler))
    res = await web.info.news({}, "it")
    assert not res["ok"]


# -- registry and prompt -------------------------------------------------------------------

async def test_registry_without_home_assistant(web):
    reg = ToolRegistry(None, web.info)
    assert await reg.start()
    assert [s["function"]["name"] for s in reg.specs] == ["get_weather", "get_news"]
    assert reg.enabled and not reg.home_enabled and reg.wants_position
    call = ToolCall("1", "get_weather", {"location": "Roma"})
    assert reg.risk(call) is None and not reg.is_action("get_weather")
    res = await reg.execute(call, "it")
    assert res["ok"] and res["place"].startswith("Roma")
    set_tool_context(reg)
    sp = system_prompt(True)
    assert "get_weather" in sp and "act on the user's home" not in sp


async def test_registry_with_home_assistant_and_away(web, monkeypatch):
    home = FakeHome()
    home.services = {"google_assistant_sdk.send_text_command"}
    reg = ToolRegistry(home, web.info)
    await reg.start()
    names = [s["function"]["name"] for s in reg.specs]
    assert names == ["intent__HassTurnOff", "google_assistant", "get_weather", "get_news"]
    assert [s["function"]["name"] for s in reg.home_specs] == ["intent__HassTurnOff", "google_assistant"]
    assert reg.is_action("intent__HassTurnOff")
    set_tool_context(reg)
    assert "act on the user's home" in system_prompt(True) and "Luce cucina" in system_prompt(True)
    monkeypatch.setattr(cfg, "home_route", "google")
    assert await reg.update_route()
    assert [s["function"]["name"] for s in reg.specs] == ["google_assistant", "get_weather", "get_news"]


async def test_registry_when_home_assistant_is_down(web):
    class DownHome(FakeHome):
        async def list_tools(self):
            raise httpx.ConnectError("down")

    reg = ToolRegistry(DownHome(), web.info)
    assert not await reg.start()
    assert [s["function"]["name"] for s in reg.specs] == ["get_weather", "get_news"]
    assert not reg.home_enabled


def test_no_date_in_the_prompt():
    # With a date in the messages, Qwen answers weather and news itself instead of calling the tools.
    msgs = build_messages([{"role": "user", "content": "che tempo fa?"}], "it", with_tools=True)
    assert msgs[-1]["content"] == "che tempo fa?\n\n(Reply in italiano.)"
    assert "2026" not in msgs[0]["content"]


# -- in a conversation ---------------------------------------------------------------------

class WeatherLlm:
    """Round 1: get_weather for the user's position. Round 2: speaks the tool result."""

    name = "fake"
    last_fallback = None
    using_remote = False

    def __init__(self):
        self.calls = []

    def set_tools(self, tools):
        self.tools = tools

    async def prefill(self, messages):
        pass

    async def stream(self, messages):
        self.calls.append(messages)
        await asyncio.sleep(0.01)
        if messages[-1]["role"] == "tool":
            res = json.loads(messages[-1]["content"])
            yield f"A {res['place']} ci sono {res['now']['temp_c']} gradi."
            return
        yield ToolCall("w1", "get_weather", {})


@pytest.fixture
def chat(web, monkeypatch):
    sessions = []
    svc = Services(stt=FakeStt(), llm=WeatherLlm(), tts=FakeTts(), langid=LangId(), tools=ToolRegistry(None, web.info))
    monkeypatch.setattr(cfg, "warmup", False)
    monkeypatch.setattr(cfg, "local_hosts", [*cfg.local_hosts, "testserver"])
    monkeypatch.setattr(main, "build_services", lambda: svc)
    orig = Session.__init__

    def init(self, ws, s):
        orig(self, ws, s)
        sessions.append(self)

    monkeypatch.setattr(Session, "__init__", init)
    with TestClient(main.app) as c:
        c.sessions, c.web = sessions, web
        yield c


def test_weather_here_in_a_chat(chat):
    with chat.websocket_connect("/ws") as ws:
        ready = recv(ws)
        assert ready["location"] is True and ready["providers"]["home_tools"] == 0
        ws.send_text(json.dumps({"type": "location", "lat": "not a number", "lon": 9}))
        ws.send_text(json.dumps({"type": "location", "lat": 45.4642, "lon": 9.19}))
        ws.send_text(json.dumps({"type": "text", "text": "che tempo fa qui?"}))
        msgs = recv_until(ws, lambda m: m["type"] == "turn_end")
    assert chat.sessions[0].position == (45.4642, 9.19)
    assert [m["text"] for m in msgs if m["type"] == "sentence"] == ["A Milano ci sono 13 gradi."]
    tool = next(m for m in msgs if m["type"] == "tool")
    assert tool["calls"] == [{"name": "get_weather", "args": {}}]
    assert "ipwho.is" not in chat.web.hosts()


def test_phone_command_with_position(chat, monkeypatch):
    token = "k" * 40
    monkeypatch.setattr(cfg, "api_tokens", [token])
    r = chat.post("/api/command", json={"text": "che tempo fa?", "lat": 45.4642, "lon": 9.19},
                  headers={"Authorization": f"Bearer {token}"})
    body = r.json()
    assert body["status"] == "done" and body["reply"] == "A Milano ci sono 13 gradi."
    assert body["actions"] == []  # a lookup, not something done in the house
    bad = chat.post("/api/command", json={"text": "che tempo fa?", "lat": 123, "lon": 9},
                    headers={"Authorization": f"Bearer {token}"})
    assert bad.status_code == 422


@pytest.mark.parametrize("text,tool", [
    ("Che tempo fa qui?", "get_weather"), ("E domani piove?", "get_weather"), ("Quanti gradi ci sono fuori?", "get_weather"),
    ("What's the weather in London?", "get_weather"), ("Will it rain tomorrow?", "get_weather"),
    ("Quel temps fait-il ? Il pleut ?", "get_weather"), ("Wie wird das Wetter morgen?", "get_weather"),
    ("¿Qué tiempo hace en Madrid?", "get_weather"), ("Vai chover amanhã?", "get_weather"), ("Wordt het morgen regen?", "get_weather"),
    ("Dammi le ultime notizie", "get_news"), ("Any news today?", "get_news"), ("Was gibt es für Nachrichten?", "get_news"),
    ("Tre anni tempo fa ero a Roma", None), ("¿Cuánto tiempo tarda?", None), ("Che temperatura c'è in salotto?", None),
    ("Accendi la luce", None), ("Qual è la capitale del Portogallo?", None), (None, None),
])
def test_route(text, tool):
    from server.info import route

    assert route(text) == tool


async def test_hint_names_the_tool_only_in_the_message(web):
    reg = ToolRegistry(None, web.info)
    await reg.start()
    assert reg.hint("che tempo fa?") == "get_weather" and reg.hint("accendi la luce") is None
    history = [{"role": "user", "content": "che tempo fa?"}]
    msgs = build_messages(history, "it", with_tools=True, hint=reg.hint("che tempo fa?"))
    assert msgs[-1]["content"] == "che tempo fa?\n\n(Call get_weather for this.)\n\n(Reply in italiano.)"
    assert history == [{"role": "user", "content": "che tempo fa?"}]
    assert ToolRegistry().hint("che tempo fa?") is None  # weather tool off
