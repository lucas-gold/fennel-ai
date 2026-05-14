"""The only code in this project that touches the network.

Everything here is gated behind an explicit user opt-in (see `briefing.py`) and
runs at most once a day. Deliberate constraints:

- **stdlib only.** urllib + ElementTree, no requests/feedparser. One fewer
  dependency to ship, audit and licence-check.
- **Fixed source list.** The daily fetch never includes anything the user typed,
  so it leaks nothing about them — unlike live search, which by definition sends
  their question to a third party. The two are separate settings for that reason.
- **Feed metadata only.** We keep the title, the feed's own short description,
  and the link. Full article text is not fetched or stored: redistributing it
  is a copyright question we have no reason to take on (SHIPPING.md).
- **Hard timeouts, and failure is never fatal.** A dead feed degrades the
  briefing; it must not delay or break a voice assistant.
"""
from __future__ import annotations

import html
import json
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Optional

TIMEOUT = 8
_UA = "my_ai/0.1 (local assistant; daily briefing)"

DEFAULT_FEEDS = [
    ("BBC World", "https://feeds.bbci.co.uk/news/world/rss.xml"),
    ("NPR", "https://feeds.npr.org/1001/rss.xml"),
    ("Ars Technica", "https://feeds.arstechnica.com/arstechnica/index"),
]

_WMO = {
    0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
    45: "foggy", 48: "freezing fog", 51: "light drizzle", 53: "drizzle",
    55: "heavy drizzle", 61: "light rain", 63: "rain", 65: "heavy rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 80: "rain showers",
    81: "rain showers", 82: "violent rain showers", 95: "thunderstorms",
    96: "thunderstorms with hail", 99: "thunderstorms with hail",
}


@dataclass
class Item:
    source: str
    title: str
    summary: str
    link: str


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read()


def _text(s: Optional[str], limit: int) -> str:
    if not s:
        return ""
    if "<" in s and ">" in s:
        # Feed descriptions carry HTML, and plenty of it isn't well-formed XML.
        try:
            s = "".join(ET.fromstring(f"<x>{s}</x>").itertext())
        except ET.ParseError:
            s = re.sub(r"<[^>]+>", " ", s)
    flat = " ".join(html.unescape(s).split())
    return flat[:limit].rstrip()


def geocode(place: str) -> Optional[tuple[float, float, str]]:
    """City name → coordinates. The user types this in Settings, so we never
    read the machine's actual location."""
    if not place.strip():
        return None
    q = urllib.parse.urlencode({"name": place.strip(), "count": 1, "language": "en"})
    try:
        data = json.loads(_get(f"https://geocoding-api.open-meteo.com/v1/search?{q}"))
        hit = (data.get("results") or [None])[0]
        if not hit:
            return None
        label = ", ".join(x for x in (hit.get("name"), hit.get("country_code")) if x)
        return float(hit["latitude"]), float(hit["longitude"]), label
    except Exception as exc:
        print(f"[feeds] geocode failed: {exc}", flush=True)
        return None


def weather(lat: float, lon: float, label: str) -> Optional[str]:
    """Today's forecast as hour-by-hour lines, not a snapshot.

    The briefing is fetched once and then sits in the prompt all day, so a
    "current temperature" captured at fetch time is wrong within the hour — it
    was reporting the overnight low as the current temp in the afternoon.
    Instead we ship the whole day's hourly series and let the model read off the
    row matching the clock in its `<context>` block.

    Open-Meteo: free, no key, no account.
    """
    q = urllib.parse.urlencode({
        "latitude": lat, "longitude": lon, "timezone": "auto",
        "hourly": "temperature_2m,precipitation_probability,weather_code",
        "daily": "temperature_2m_max,temperature_2m_min,"
                 "precipitation_probability_max,precipitation_sum,weather_code,sunrise,sunset",
        "forecast_days": 1,
    })
    try:
        d = json.loads(_get(f"https://api.open-meteo.com/v1/forecast?{q}"))
        day, hourly = d["daily"], d["hourly"]
        unit = d.get("daily_units", {}).get("temperature_2m_max", "°C")
        lines = [
            f"Weather in {label} today: {_WMO.get(day['weather_code'][0], 'mixed')}, "
            f"high {round(day['temperature_2m_max'][0])}{unit}, "
            f"low {round(day['temperature_2m_min'][0])}{unit}, "
            f"{day['precipitation_probability_max'][0]}% chance of precipitation"
            f" ({day['precipitation_sum'][0]} mm expected)."
            f" Sunrise {day['sunrise'][0][-5:]}, sunset {day['sunset'][0][-5:]}.",
            "Hour by hour (local time, temp, chance of precipitation, conditions) —"
            " read the row matching the current time rather than assuming:",
        ]
        rows = []
        for t, temp, pop, code in zip(hourly["time"], hourly["temperature_2m"],
                                      hourly["precipitation_probability"],
                                      hourly["weather_code"]):
            rows.append(f"{t[-5:]} {round(temp)}{unit} {pop}% {_WMO.get(code, 'mixed')}")
        lines.append("; ".join(rows))
        return "\n".join(lines)
    except Exception as exc:
        print(f"[feeds] weather failed: {exc}", flush=True)
        return None


def wiki_search(query: str, limit: int = 2) -> list[Item]:
    """Look something up on Wikipedia. Free, keyless, no account, no quota.

    This is an encyclopedia lookup, not a general web search — it won't find
    "best pizza near me" or this morning's news. But it does cover the large
    class of questions a small local model gets wrong or is out of date on
    (people, places, definitions, history), which is most of what "search the
    internet" is actually asked for here.

    One request: `generator=search` feeds the search hits straight into
    `prop=extracts`, so we get ranked results *and* their intro text together.
    Content is CC BY-SA, so the source and link travel with it.
    """
    q = urllib.parse.urlencode({
        "action": "query", "format": "json", "prop": "extracts|info",
        "inprop": "url", "exintro": 1, "explaintext": 1, "exchars": 700,
        "generator": "search", "gsrsearch": query.strip(), "gsrlimit": limit,
    })
    try:
        data = json.loads(_get(f"https://en.wikipedia.org/w/api.php?{q}"))
    except Exception as exc:
        print(f"[feeds] wiki search failed: {exc}", flush=True)
        return []
    pages = (data.get("query") or {}).get("pages") or {}
    # `index` preserves the search ranking; dict order here does not.
    ranked = sorted(pages.values(), key=lambda p: p.get("index", 99))
    out: list[Item] = []
    for p in ranked[:limit]:
        extract = _text(p.get("extract"), 700)
        if not extract:
            continue
        out.append(Item(source="Wikipedia", title=_text(p.get("title"), 120),
                        summary=extract, link=p.get("fullurl", "")))
    return out


def headlines(feeds: Optional[list[tuple[str, str]]] = None,
              per_feed: int = 8) -> list[Item]:
    out: list[Item] = []
    for name, url in (feeds or DEFAULT_FEEDS):
        try:
            root = ET.fromstring(_get(url))
        except Exception as exc:
            print(f"[feeds] {name} failed: {exc}", flush=True)
            continue
        # RSS 2.0 (channel/item) and Atom (entry) both show up in the wild.
        nodes = root.findall(".//item") or root.findall(
            ".//{http://www.w3.org/2005/Atom}entry")
        for n in nodes[:per_feed]:
            def find(tag: str) -> Optional[str]:
                # `or` is wrong here: an Element with no children is falsy, so a
                # perfectly good <title>text</title> would be discarded.
                el = n.find(tag)
                if el is None:
                    el = n.find(f"{{http://www.w3.org/2005/Atom}}{tag}")
                return el.text if el is not None else None
            title = _text(find("title"), 160)
            if not title:
                continue
            out.append(Item(
                source=name,
                title=title,
                summary=_text(find("description") or find("summary"), 240),
                link=(find("link") or "").strip(),
            ))
    return out
