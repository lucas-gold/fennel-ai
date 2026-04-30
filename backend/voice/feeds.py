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
    """One line of today's weather. Open-Meteo: free, no key, no account."""
    q = urllib.parse.urlencode({
        "latitude": lat, "longitude": lon, "timezone": "auto",
        "current": "temperature_2m,weather_code",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code",
        "forecast_days": 1,
    })
    try:
        d = json.loads(_get(f"https://api.open-meteo.com/v1/forecast?{q}"))
        cur, day = d["current"], d["daily"]
        unit = d.get("current_units", {}).get("temperature_2m", "°C")
        return (f"Weather in {label}: {_WMO.get(cur['weather_code'], 'mixed')}, "
                f"now {round(cur['temperature_2m'])}{unit}, "
                f"high {round(day['temperature_2m_max'][0])}{unit} / "
                f"low {round(day['temperature_2m_min'][0])}{unit}, "
                f"{day['precipitation_probability_max'][0]}% chance of precipitation.")
    except Exception as exc:
        print(f"[feeds] weather failed: {exc}", flush=True)
        return None


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
