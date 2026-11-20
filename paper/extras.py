"""Section kinds that aren't articles: your tasks, the weather, market numbers and short daily prompts.

Each function takes a slot (its settings come from the plan) and returns {"items": [...]}.
Nothing here is specific to one reader: cities, symbols, task projects and prompt ideas all come
from the slot's own settings.
"""
from __future__ import annotations

import os
import re
from datetime import date as Date, datetime, timedelta, timezone
from pathlib import Path

from . import fetchers
from .llm import LLMUnavailable

import logging

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------- tasks (a local task app)


def _read_env(path: str) -> dict:
    out = {}
    p = Path(os.path.expanduser(path))
    if not p.exists():
        return out
    for line in p.read_text().splitlines():
        m = re.match(r"\s*([A-Z0-9_]+)\s*=\s*(.*)\s*$", line)
        if m:
            out[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    return out


def tasks(R, slot: dict) -> dict:
    """Open tasks from a local task app with a small JSON API (GET /items?item_type=project, GET /tasks): what's due today or overdue in one project, plus a count
    of completed tasks whose titles match a pattern (e.g. applications sent)."""
    env = _read_env(slot.get("env_file", ""))
    token_var = slot.get("token_var") or "TASKS_API_TOKEN"
    token = os.environ.get(token_var) or env.get(token_var, "")
    base = (slot.get("base_url") or env.get(slot.get("base_url_var") or "TASKS_API_BASE_URL") or "http://127.0.0.1:8000").rstrip("/")
    if not token:
        return {"items": [{"kind": "note", "text": "Couldn't find the task app's API token. Check the env file path in this section's settings."}]}
    headers = {"Authorization": f"Bearer {token}"}

    def get_json(path, **params):
        r = fetchers.session.get(base + path, headers=headers, params=params, timeout=10)
        if r.status_code >= 400:
            raise fetchers.FetchError(f"HTTP {r.status_code} from the task app {path}")
        return r.json()

    try:
        project_id = None
        name = (slot.get("project") or "").strip().lower()
        if name:
            projects = get_json("/items", item_type="project", limit=100).get("items", [])
            match = next((p for p in projects if p["title"].strip().lower() == name), None) or \
                next((p for p in projects if name in p["title"].lower()), None)
            if not match:
                return {"items": [{"kind": "note", "text": f"No project called “{slot.get('project')}”."}]}
            project_id = match["id"]
        params = {"status": "open", "limit": 100}
        if project_id:
            params["project_id"] = project_id
        open_tasks = get_json("/tasks", **params).get("items", [])
        done_params = {**params, "status": "completed"}
        done = get_json("/tasks", **done_params).get("items", [])
    except Exception as e:  # noqa: BLE001 - the app may simply not be running
        R.warn(f"{slot['slot']}: task app not reachable ({e})")
        return {"items": [{"kind": "note", "text": "The task app wasn't running when the paper was made, so today's tasks are missing."}]}

    today = R.day.isoformat()
    due = [t for t in open_tasks if t.get("due_date") and t["due_date"] <= today]
    upcoming = [t for t in open_tasks if not t.get("due_date")][: int(slot.get("item_limit") or 6)]
    items = []
    for t in due[:12]:
        items.append({"kind": "task", "title": t["title"], "overdue": t["due_date"] < today, "due_date": t["due_date"]})
    if not due:
        for t in upcoming:
            items.append({"kind": "task", "title": t["title"], "overdue": False, "due_date": None})
    pattern = slot.get("count_pattern")
    if pattern:
        rx = re.compile(pattern, re.I)
        since = slot.get("count_since") or (R.day - timedelta(days=7)).isoformat()
        n_total = sum(1 for t in done if rx.search(t["title"]) and (t.get("completed_at") or "")[:10] >= since)
        n_week = sum(1 for t in done if rx.search(t["title"]) and (t.get("completed_at") or "")[:10] >= (R.day - timedelta(days=7)).isoformat())
        items.append({"kind": "count", "label": slot.get("count_label") or "Done", "total": n_total, "since": since,
                      "last_7_days": n_week})
    return {"items": items}


# ---------------------------------------------------------------------------- weather (Open-Meteo, no key)

WMO = {0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "fog", 51: "light drizzle",
       53: "drizzle", 55: "heavy drizzle", 61: "light rain", 63: "rain", 65: "heavy rain", 71: "light snow", 73: "snow",
       75: "heavy snow", 80: "showers", 81: "showers", 82: "heavy showers", 95: "thunderstorms", 96: "thunderstorms",
       99: "thunderstorms"}


def weather(R, slot: dict) -> dict:
    places = [p for p in (slot.get("places") or []) if p]
    if not places:
        return {"items": [{"kind": "note", "text": "Tell the paper your city (this section's settings) to get the weather."}]}
    items = []
    for place in places[:4]:
        try:
            g = fetchers.get("https://geocoding-api.open-meteo.com/v1/search", params={"name": place, "count": 1}).json()
            loc = (g.get("results") or [None])[0]
            if not loc:
                items.append({"kind": "note", "text": f"Couldn't find “{place}”."})
                continue
            f = fetchers.get("https://api.open-meteo.com/v1/forecast", params={
                "latitude": loc["latitude"], "longitude": loc["longitude"], "timezone": "auto", "forecast_days": 1,
                "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,wind_speed_10m_max,sunrise",
                "hourly": "temperature_2m,precipitation_probability"}).json()
            d = f["daily"]
            hourly = f.get("hourly", {})
            early = [(t, temp) for t, temp in zip(hourly.get("time", []), hourly.get("temperature_2m", []))
                     if t[11:13] in ("05", "06", "07")]
            items.append({"kind": "weather", "place": loc["name"], "summary": WMO.get(d["weather_code"][0], "mixed"),
                          "high": d["temperature_2m_max"][0], "low": d["temperature_2m_min"][0],
                          "rain_chance": d["precipitation_probability_max"][0], "wind": d["wind_speed_10m_max"][0],
                          "sunrise": (d.get("sunrise") or [""])[0][11:16],
                          "early_run": round(sum(t for _, t in early) / len(early)) if early else None,
                          "url": f"https://open-meteo.com/en/docs#latitude={loc['latitude']}&longitude={loc['longitude']}",
                          "publisher": "Open-Meteo"})
        except (fetchers.FetchError, KeyError, ValueError, IndexError, TypeError) as e:
            R.warn(f"Weather for {place} failed: {e}")
    return {"items": items}


# ---------------------------------------------------------------------------- markets


def markets(R, slot: dict) -> dict:
    """Last close and day change for a few symbols (Yahoo Finance chart API), plus USD/INR."""
    items = []
    for s in slot.get("symbols") or []:
        try:
            r = fetchers.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{s['symbol']}",
                             params={"range": "5d", "interval": "1d"})
            res = r.json()["chart"]["result"][0]
            closes = [c for c in res["indicators"]["quote"][0]["close"] if c is not None]
            last, prev = closes[-1], closes[-2] if len(closes) > 1 else None
            items.append({"kind": "quote", "label": s["label"], "value": round(last, 2),
                          "change_pct": round((last - prev) / prev * 100, 2) if prev else None,
                          "url": f"https://finance.yahoo.com/quote/{s['symbol']}", "publisher": "Yahoo Finance"})
        except (fetchers.FetchError, KeyError, IndexError, ValueError, TypeError, ZeroDivisionError) as e:
            R.warn(f"{s.get('label')}: market data unavailable ({e})")
    if slot.get("usd_inr", True):
        try:
            rate = float(fetchers.get("https://open.er-api.com/v6/latest/USD").json()["rates"]["INR"])
            items.append({"kind": "quote", "label": "USD/INR", "value": round(rate, 2), "change_pct": None,
                          "url": "https://www.exchangerate-api.com", "publisher": "ExchangeRate-API"})
        except (fetchers.FetchError, KeyError, ValueError, TypeError) as e:
            R.warn(f"USD/INR unavailable ({e})")
    return {"items": items}


# ---------------------------------------------------------------------------- daily prompts


def prompt(R, slot: dict) -> dict:
    """One short, specific nudge for the day, written from the reader's goals; falls back to the
    slot's own list of ideas."""
    ideas = slot.get("ideas") or []
    fallback = ideas[R.day.toordinal() % len(ideas)] if ideas else ""
    if R.llm.available:
        schema = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"], "additionalProperties": False}
        try:
            out = R.llm.json(
                "You write one short daily nudge for a personal newspaper. Plain words, specific, no fluff, under 40 words.",
                f"Section: {slot['slot']}\nPurpose: {slot.get('purpose', '')}\nIdeas to draw on: {ideas}\n"
                f"Reader's goals: {R.profile['goals']}\nReader's interests: {R.profile['interests']}\n"
                f"Today is {R.day.strftime('%A %d %B %Y')}. Write today's nudge. Don't repeat yesterday's wording.",
                schema, max_tokens=400, effort="low")
            if out.get("text"):
                return {"items": [{"kind": "prompt", "text": out["text"].strip()}]}
        except (LLMUnavailable, KeyError, ValueError) as e:
            R.warn(f"{slot['slot']}: used a stock prompt ({e})")
    return {"items": [{"kind": "prompt", "text": fallback}]} if fallback else {"items": []}


# ---------------------------------------------------------------------------- business concept + real case


def C(name, search=None, wikipedia=None):
    return {"name": name, "search": search, "wikipedia": wikipedia}


# Standard business concepts, used when a concept slot doesn't list its own.
DEFAULT_CONCEPTS = [
    C("Unit economics", '"unit economics" startup'),
    C("Customer acquisition cost", '"customer acquisition cost" OR "CAC" startup', "Customer acquisition cost"),
    C("Customer lifetime value", '"lifetime value" customers company', "Customer lifetime value"),
    C("Contribution margin", '"contribution margin" company'),
    C("Gross margin", '"gross margin" company results'),
    C("Operating leverage", '"operating leverage" company'),
    C("Churn rate", '"churn" subscribers company', "Churn rate"),
    C("Network effect", '"network effects" platform company', "Network effect"),
    C("Switching costs", '"switching costs" customers company', "Switching barriers"),
    C("Economies of scale", '"economies of scale" company'),
    C("Pricing power", '"pricing power" company'),
    C("Take rate", '"take rate" marketplace OR platform', "Two-sided market"),
    C("Freemium", 'freemium "paid users" OR "conversion"'),
    C("Recurring revenue", '"annual recurring revenue" OR "ARR" startup', "Annual recurring revenue"),
    C("Burn rate and runway", '"cash burn" OR "burn rate" startup', "Burn rate"),
    C("Working capital", '"working capital" company', "Working capital"),
    C("Economic moat", '"moat" company competitors', "Economic moat"),
    C("Product-market fit", '"product-market fit" startup'),
    C("Bundling", 'bundle subscription company "bundling"', "Product bundling"),
    C("Vertical integration", '"vertical integration" company'),
    C("Distribution channel", '"distribution" channel brand company sales', "Distribution (marketing)"),
    C("Labour arbitrage", '"outsourcing" India services clients revenue', "Labor arbitrage"),
]


def concept(R, slot: dict) -> dict:
    """One business concept a day, explained through a real company in the news.

    The slot lists concepts as {"name": ..., "search": optional news query}. The definition comes from
    Wikipedia, the case from a recent news story; the writer only uses what was fetched."""
    concepts = slot.get("concepts") or DEFAULT_CONCEPTS
    c = concepts[R.day.toordinal() % len(concepts)]
    name = c["name"] if isinstance(c, dict) else str(c)
    query = (c.get("search") if isinstance(c, dict) else None) or f'"{name}" company'
    R.step(f"Concept of the day: {name}")

    wiki = None
    try:
        wiki = (fetchers.fetch_wikipedia(c.get("wikipedia") or name, limit=1) or [None])[0]
    except (fetchers.FetchError, KeyError, ValueError) as e:
        R.warn(f"Concept {name}: Wikipedia unavailable ({e})")
    cases = []
    try:
        url = f"https://news.google.com/rss/search?q={fetchers.quote_plus(query)}+when:30d&hl=en-IN&gl=IN&ceid=IN:en"
        cases = fetchers.fetch_gnews(url, limit=12)
    except fetchers.FetchError as e:
        R.warn(f"Concept {name}: no news cases ({e})")
    if not wiki and not cases:
        return {"items": []}

    item = {"kind": "concept", "concept": name, "headline": name, "explanation": "", "case_headline": "",
            "case_story": "", "takeaway": "", "url": wiki["url"] if wiki else "", "publisher": "Wikipedia" if wiki else "",
            "case_url": "", "case_publisher": "", "case_published_at": None}
    pick = cases[0] if cases else None
    if R.llm.available and cases:
        listing = [{"id": i, "title": x["title"], "publisher": x["publisher"]} for i, x in enumerate(cases)]
        schema = {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"], "additionalProperties": False}
        try:
            got = R.llm.json("You pick news stories that show a business concept at work in a real company.",
                             f"Concept: {name}\nPick the story that most clearly shows this concept in one named company. "
                             f"Prefer concrete numbers. A story that only shares a word with the concept (a company "
                             f"named after it, say) doesn't count. Return id -1 if none really fits.\n{listing}",
                             schema, max_tokens=300, effort="low")
            pick = cases[got["id"]] if 0 <= got.get("id", -1) < len(cases) else None
        except (LLMUnavailable, KeyError, ValueError, TypeError) as e:
            R.warn(f"Concept {name}: picked the newest case ({e})")
    article = ""
    if pick:
        try:
            art = fetchers.fetch_article(pick["url"])
            article = (art.get("text") or art.get("description") or "")[:6000]
            if art.get("url"):
                pick["url"] = art["url"]
        except Exception:  # noqa: BLE001 - the headline alone still works
            pass
        item.update(case_headline=pick["title"], case_url=pick["url"], case_publisher=pick["publisher"],
                    case_published_at=pick.get("published_at"))

    if R.llm.available:
        schema = {"type": "object", "properties": {
            "headline": {"type": "string"}, "explanation": {"type": "string"},
            "case_story": {"type": "string"}, "takeaway": {"type": "string"}},
            "required": ["headline", "explanation", "case_story", "takeaway"], "additionalProperties": False}
        try:
            out = R.llm.json(
                "You explain one business concept a day to a curious reader, then show it in a real company. Plain words, "
                "short sentences, numbers where the source has them. Use only the material given; never invent figures.",
                f"Concept: {name}\nDefinition source (Wikipedia):\n{(wiki or {}).get('text', '')[:3000]}\n\n"
                + (f"Real case: {item['case_headline']} ({item['case_publisher']})\n{article}\n\n" if pick else
                   "No news case today: use a well-known, widely documented company example and say it's a classic "
                   "example, without precise figures.\n\n") +
                f"Reader's goals: {R.profile['goals']}\n\n"
                "headline: 'Concept, via Company' style, under 10 words. explanation: what the concept is and why it "
                "matters, under 70 words. case_story: how it shows up in this company, under 90 words, only facts from the "
                "case text. takeaway: one sentence the reader can use when sizing up a business.",
                schema, max_tokens=1500, effort="medium")
            item.update({k: out[k].strip() for k in schema["required"]})
            return {"items": [item]}
        except (LLMUnavailable, KeyError, ValueError, AttributeError) as e:
            R.warn(f"Concept {name}: written without AI ({e})")
            log.warning("concept writer failed: %s", e)
    if wiki:
        sentences = re.split(r"(?<=[.!?])\s+", wiki.get("text", ""))
        item["explanation"] = " ".join(sentences[:2])
    return {"items": [item]}


KINDS = {"tasks": tasks, "weather": weather, "markets": markets, "prompt": prompt, "concept": concept}
