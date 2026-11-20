"""Stage B: run the saved plan and publish one edition.

fetch candidates → filter & group → select → compose → validate → publish
A failed or repeated run never touches an already-published issue.
"""
from __future__ import annotations

import json
import logging
import math
import re
import time
import traceback
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor
from datetime import date as Date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import fetchers, maths
from .db import DB
from .llm import LLM, LLMUnavailable

log = logging.getLogger(__name__)

WPM = 220
DEFAULT_USD_INR = 93.3

STOP = set("a an the of to in on for and or with by at from as is are was were be after over amid into its it this that "
           "says said say new will may could after vs".split())

CONSEQUENCE_WORDS = {
    "election": 3, "vote": 2, "parliament": 2, "lok sabha": 2, "supreme court": 3, "court": 1, "rbi": 3, "gdp": 3,
    "inflation": 3, "war": 3, "ceasefire": 3, "summit": 2, "tariff": 3, "budget": 3, "policy": 2, "sanction": 3,
    "government": 1, "minister": 1, "prime minister": 2, "president": 1, "bill": 2, "law": 2, "rupee": 2,
    "interest rate": 3, "rate": 1, "trade": 2, "treaty": 2, "nuclear": 3, "oil": 2, "attack": 2, "earthquake": 3,
    "flood": 2, "cyclone": 3, "strike": 1, "investors": 1, "economy": 2, "exports": 2, "imports": 1, "jobs": 2,
    "declaration": 2, "visit": 1, "agreement": 2, "deal": 1, "fed": 2, "markets": 1,
}
TRIVIAL_WORDS = ["cricket", "film", "actor", "actress", "celebrity", "murder", "arrested", "convocation", "festival",
                 "horoscope", "recipe", "photos", "watch:", "video", "quiz", "graphic memoir", "dancing"]


# ----------------------------------------------------------------------------- helpers

def today_in(tz: str) -> str:
    return datetime.now(ZoneInfo(tz)).date().isoformat()


def tokens(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", s.lower()) if w not in STOP and len(w) > 2}


def similar(a: str, b: str) -> float:
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def words(*texts) -> int:
    return sum(len(str(t or "").split()) for t in texts)


def sentences(text: str, n: int = 2) -> str:
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z“\"'])", (text or "").strip())
    return " ".join(p for p in parts[:n] if p).strip()


def hours_old(iso: str | None, now: datetime) -> float | None:
    if not iso:
        return None
    try:
        d = datetime.fromisoformat(iso)
    except ValueError:
        return None
    return (now - d).total_seconds() / 3600


def runs_on(slot: dict, day: Date) -> bool:
    f = slot.get("frequency", "daily")
    if f == "weekends":
        return day.weekday() >= 5
    if f == "weekdays":
        return day.weekday() < 5
    if f == "weekly":
        return day.weekday() == 6
    return True


class Run:
    def __init__(self, db: DB, profile: dict, plan: dict, edition_date: str, llm: LLM, now: datetime):
        self.db = db
        self.profile = profile
        self.plan = plan
        self.date = edition_date
        self.day = Date.fromisoformat(edition_date)
        self.llm = llm
        self.now = now
        self.diag = {"llm": llm.name, "llm_error": llm.last_error, "sources": [], "steps": [], "validation": []}
        self.past = db.past_sources(days=30)

    def step(self, msg: str):
        log.info(msg)
        self.diag["steps"].append({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "msg": msg})

    def warn(self, msg: str):
        self.diag["validation"].append(msg)

    # ------------------------------------------------------------------ fetch

    def fetch_source(self, slot: dict, src: dict, url_override: str | None = None) -> list[dict]:
        rec = {"slot": slot["id"], "source": src.get("name"), "method": src.get("method"), "url": url_override or src.get("url"),
               "status": "ok", "count": 0, "error": "", "fallback_used": False, "seconds": 0.0}
        t0 = time.time()
        items: list[dict] = []
        try:
            items = self._fetch_by_method(src.get("method"), url_override or src.get("url"), src)
            if not items:
                raise fetchers.FetchError("No items found")
        except Exception as e:  # noqa: BLE001 - record and try the fallback
            rec["status"] = "failed"
            rec["error"] = str(e)[:300]
            if src.get("fallback_method") and src.get("fallback_url"):
                try:
                    items = self._fetch_by_method(src["fallback_method"], src["fallback_url"], src)
                    rec["fallback_used"] = True
                    rec["status"] = "fallback" if items else "failed"
                except Exception as e2:  # noqa: BLE001
                    rec["error"] += f" · fallback: {str(e2)[:200]}"
        rec["count"] = len(items)
        rec["seconds"] = round(time.time() - t0, 1)
        if items:
            rec["direct_links"] = sum(1 for i in items if i.get("direct_link"))
        self.diag["sources"].append(rec)
        for i in items:
            i["source_name"] = src.get("name")
            i["slot_id"] = slot["id"]
        return items

    def _fetch_by_method(self, method: str, url: str, src: dict) -> list[dict]:
        publisher = re.sub(r"\s+—.*$", "", src.get("name", "")).strip()
        if method == "rss":
            return fetchers.fetch_rss(url, publisher)
        if method == "gnews":
            return fetchers.fetch_gnews(url, publisher, limit=30, decode=False)
        if method == "page":
            return fetchers.fetch_page_links(url, publisher, src.get("link_pattern", ""))
        raise fetchers.FetchError(f"Method {method!r} is not usable here")

    def fetch_slot(self, slot: dict) -> list[dict]:
        items: list[dict] = []
        srcs = [s for s in slot.get("sources", []) if s.get("method") not in ("featured", "gnews_search", "wikipedia")]
        with ThreadPoolExecutor(max_workers=6) as pool:
            for res in pool.map(lambda s: self.fetch_source(slot, s), srcs):
                items.extend(res)
        return items

    # ------------------------------------------------------------------ filter & group

    def filter_group(self, slot: dict, items: list[dict], drop_trivial: bool = False) -> list[dict]:
        fresh_h = slot.get("freshness_hours") or 0
        kept = []
        for it in items:
            if not it.get("title") or not it.get("url"):
                continue
            age = hours_old(it.get("published_at"), self.now)
            it["age_hours"] = None if age is None else round(age, 1)
            if fresh_h and age is not None and age > fresh_h:
                continue
            if drop_trivial and any(w in it["title"].lower() for w in TRIVIAL_WORDS):
                continue
            kept.append(it)
        # Merge duplicate coverage across sources.
        groups: list[dict] = []
        for it in kept:
            for g in groups:
                if similar(g["title"], it["title"]) >= 0.5:
                    g.setdefault("also", []).append({"publisher": it["publisher"], "url": it["url"], "title": it["title"]})
                    if (not g.get("summary")) and it.get("summary"):
                        g["summary"] = it["summary"]
                    break
            else:
                groups.append(it)
        # Compare with past editions.
        for g in groups:
            for p in self.past:
                if p["url"] == g["url"] or similar(p["title"], g["title"]) >= 0.55:
                    g["seen_before"] = {"date": p["date"], "title": p["title"]}
                    break
        return groups

    # ------------------------------------------------------------------ news

    def news(self, slot: dict) -> dict:
        self.step(f"Fetching {slot['slot']}")
        raw = self.fetch_slot(slot)
        cands = self.filter_group(slot, raw, drop_trivial=True)
        self.step(f"{len(raw)} candidates, {len(cands)} after filtering and merging")
        limit = int(slot.get("item_limit") or 3)
        picks = self._select_news(slot, cands, limit)
        # Resolve Google News links only for picked stories (cheap), and drop ones we cannot link directly
        # when a directly linked alternative exists.
        for p in picks:
            if not p.get("direct_link"):
                real = fetchers.decode_google_news_url(p["url"])
                if real:
                    p["url"], p["direct_link"] = real, True
                else:
                    self.warn(f"Could not resolve a direct link for “{p['title']}”; using the Google News link.")
        self._enrich(picks)
        items = self._compose_news(slot, picks)
        return {"items": items, "candidates": len(cands)}

    def _select_news(self, slot, cands, limit):
        if not cands:
            return []
        if self.llm.available:
            listing = [{"id": i, "title": c["title"], "publisher": c["publisher"], "published_at": c.get("published_at"),
                        "summary": (c.get("summary") or "")[:300], "also_covered_by": [a["publisher"] for a in c.get("also", [])],
                        "previously_covered": c.get("seen_before")} for i, c in enumerate(cands[:80])]
            schema = {"type": "object", "properties": {"picks": {"type": "array", "items": {"type": "object", "properties": {
                "id": {"type": "integer"}, "consequence": {"type": "integer"}, "reason": {"type": "string"},
                "is_update": {"type": "boolean"}}, "required": ["id", "consequence", "reason", "is_update"],
                "additionalProperties": False}}}, "required": ["picks"], "additionalProperties": False}
            prompt = (f"Section: {slot['section']} — {slot['slot']}\nPurpose: {slot['purpose']}\n"
                      f"Selection rule: {slot['selection_rule']}\nReader's standing coverage: {self.profile['always_cover']}\n"
                      f"Pick at most {limit} stories, fewer on a quiet day. Score consequence 1-10 (10 = changes things for "
                      f"hundreds of millions of people). Skip previously covered stories unless the new item is a material "
                      f"update (then set is_update). Order by consequence.\n\nCandidates:\n{json.dumps(listing, ensure_ascii=False)}")
            try:
                out = self.llm.json("You are a careful news editor.", prompt, schema)
                picks = []
                for p in out["picks"][:limit]:
                    if 0 <= p["id"] < len(listing):
                        c = cands[p["id"]]
                        c["consequence"] = p["consequence"]
                        c["selection_reason"] = p["reason"]
                        c["is_update"] = p["is_update"]
                        picks.append(c)
                return picks
            except (LLMUnavailable, KeyError, ValueError) as e:
                self.warn(f"AI selection failed for {slot['slot']}, used rules instead: {e}")
        scored = []
        for c in cands:
            if c.get("seen_before"):
                continue
            t = c["title"].lower() + " " + (c.get("summary") or "").lower()[:300]
            s = sum(w for k, w in CONSEQUENCE_WORDS.items() if k in t) + len(c.get("also", []))
            if c.get("direct_link"):
                s += 1
            scored.append((s, c))
        scored.sort(key=lambda x: -x[0])
        picks = []
        for s, c in scored[:limit]:
            if s < 2:
                break
            c["consequence"] = min(10, 3 + s)
            c["selection_reason"] = "Rule-based consequence score"
            picks.append(c)
        return picks

    def _enrich(self, picks):
        def one(p):
            try:
                art = fetchers.fetch_article(p["url"])
                p["text"] = p.get("text") or art["text"]
                p["published_at"] = p.get("published_at") or art["published_at"]
                p["image"] = art.get("image")
                if not p.get("summary"):
                    p["summary"] = art["description"]
            except fetchers.FetchError as e:
                p.setdefault("fetch_note", str(e))
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(one, picks))

    def _compose_news(self, slot, picks):
        if not picks:
            return []
        words_limit = int(slot.get("length_limit_words") or 60)
        if self.llm.available:
            schema = {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "object", "properties": {
                "id": {"type": "integer"}, "headline": {"type": "string"}, "brief": {"type": "string"},
                "who_it_affects": {"type": "string"}}, "required": ["id", "headline", "brief", "who_it_affects"],
                "additionalProperties": False}}}, "required": ["items"], "additionalProperties": False}
            docs = [{"id": i, "title": p["title"], "publisher": p["publisher"], "published_at": p.get("published_at"),
                     "summary": p.get("summary", "")[:800], "text": (p.get("text") or "")[:5000],
                     "is_update_of": p.get("seen_before") if p.get("is_update") else None} for i, p in enumerate(picks)]
            prompt = (f"Write a factual headline and a two-sentence brief (max {words_limit} words) for each story. "
                      "Sentence one: what happened. Sentence two: why it matters and who it affects. Use only facts in the "
                      "provided text; if the text is thin, stay close to the headline and do not add details. "
                      "For updates, start the headline with 'Update:'. who_it_affects: 3-8 words.\n\n"
                      + json.dumps(docs, ensure_ascii=False))
            try:
                out = self.llm.json("You write concise, neutral, source-grounded news briefs.", prompt, schema)
                by_id = {o["id"]: o for o in out["items"]}
                for i, p in enumerate(picks):
                    o = by_id.get(i)
                    if o:
                        p["headline"], p["brief"], p["who_it_affects"] = o["headline"], o["brief"], o["who_it_affects"]
            except (LLMUnavailable, KeyError, ValueError) as e:
                self.warn(f"AI writing failed for {slot['slot']}, used extractive briefs: {e}")
        items = []
        for p in picks:
            brief = p.get("brief") or sentences(p.get("summary") or p.get("text") or "", 2) or "Summary unavailable — follow the link to the original."
            items.append({
                "headline": p.get("headline") or p["title"],
                "brief": brief,
                "who_it_affects": p.get("who_it_affects", ""),
                "publisher": p["publisher"],
                "published_at": p.get("published_at"),
                "url": p["url"],
                "direct_link": p.get("direct_link", True),
                "also": p.get("also", [])[:3],
                "consequence": p.get("consequence", 5),
                "is_update": bool(p.get("is_update")),
                "image": p.get("image"),
                "source_title": p["title"],
            })
        return items

    # ------------------------------------------------------------------ Discover

    def explainer(self, slot: dict) -> dict:
        self.step(f"Fetching {slot['slot']}")
        raw = self.fetch_slot(slot)
        cands = [c for c in self.filter_group(slot, raw)
                 if not c.get("seen_before") and not re.search(r"& more|and more\.\.\.|quiz|weekly|wrap", c["title"], re.I)]
        if not cands:
            return {"items": []}
        pick = None
        if self.llm.available and len(cands) > 1:
            listing = [{"id": i, "title": c["title"], "publisher": c["publisher"], "published_at": c.get("published_at"),
                        "summary": (c.get("summary") or c.get("text") or "")[:400]} for i, c in enumerate(cands[:30])]
            schema = {"type": "object", "properties": {"id": {"type": "integer"}, "reason": {"type": "string"}},
                      "required": ["id", "reason"], "additionalProperties": False}
            try:
                out = self.llm.json("You pick explainers for a reader learning how businesses make money.",
                                    f"Selection rule: {slot['selection_rule']}\nReader goal: {self.profile['goals']}\n"
                                    f"Pick the single best story.\n{json.dumps(listing, ensure_ascii=False)}", schema)
                if 0 <= out["id"] < len(cands):
                    pick = cands[out["id"]]
                    pick["selection_reason"] = out["reason"]
            except (LLMUnavailable, KeyError, ValueError) as e:
                self.warn(f"AI selection failed for Discover: {e}")
        if pick is None:
            cands.sort(key=lambda c: c.get("published_at") or "", reverse=True)
            pick = cands[0]
        if not pick.get("text"):
            self._enrich([pick])
        item = {"headline": pick["title"], "publisher": pick["publisher"], "published_at": pick.get("published_at"),
                "url": pick["url"], "detail": "", "brief": "", "original_minutes": max(1, round(words(pick.get("text")) / WPM)),
                "image": pick.get("image")}
        if self.llm.available:
            schema = {"type": "object", "properties": {"brief": {"type": "string"}, "detail": {"type": "string"},
                                                       "mechanism": {"type": "string"}},
                      "required": ["brief", "detail", "mechanism"], "additionalProperties": False}
            try:
                out = self.llm.json(
                    "You write short, specific descriptions that make a reader want to click through.",
                    f"Write a description (max {slot.get('length_limit_words', 90)} words) built around the single most "
                    "interesting detail in this article, and explain the business or market mechanism it reveals. "
                    "detail: the one specific fact (with its number) you built it around. mechanism: 4-8 words naming the "
                    f"mechanism. Use only facts in the text.\n\nTitle: {pick['title']}\n\n{(pick.get('text') or pick.get('summary'))[:9000]}",
                    schema)
                item.update(brief=out["brief"], detail=out["detail"], mechanism=out["mechanism"])
            except (LLMUnavailable, KeyError, ValueError) as e:
                self.warn(f"AI writing failed for Discover: {e}")
        if not item["brief"]:
            item["brief"] = sentences(pick.get("summary") or pick.get("text") or "", 3)
        return {"items": [item]}

    # ------------------------------------------------------------------ Startup Story

    def next_rotation(self, slot: dict) -> dict | None:
        rot = slot.get("rotation") or []
        if not rot:
            return None
        recent = self.db.recent_featured(slot["id"], limit=1)
        last = recent[0]["story_key"] if recent else ""
        idx = next((i for i, r in enumerate(rot) if f"rotation:{r['business']}" == last), -1)
        return rot[(idx + 1) % len(rot)]

    def case_study(self, slot: dict) -> dict:
        rot = self.next_rotation(slot)
        self.step(f"Startup Story rotation: {rot['business'] if rot else 'none'}")
        raw: list[dict] = []
        for src in slot.get("sources", []):
            url = rot["url"] if (src.get("url") == "rotation" and rot) else src.get("url")
            raw.extend(self.fetch_source(slot, src, url_override=url))
        featured_urls = {p["url"] for p in self.db.recent_featured(slot["id"], limit=60)}
        cands = []
        for c in raw:
            if c["url"] in featured_urls or "/ideas/" in c["url"] or c["url"].rstrip("/").endswith(("/data", "/explore")):
                continue
            if not re.search(r"\$[\d.,]+\s?[KkMm]?|revenue|/month|/year", c.get("summary", "") + c["title"]):
                continue
            cands.append(c)
        if rot and rot.get("keywords"):
            kw = [k.lower() for k in rot["keywords"]]
            cands.sort(key=lambda c: -sum(k in (c["title"] + c.get("summary", "")).lower() for k in kw))
        if not cands:
            self.warn("No case study candidates found; Startup Story skipped.")
            return {"items": [], "rotation": rot}
        pick = None
        if self.llm.available and len(cands) > 1:
            listing = [{"id": i, "title": c["title"], "summary": c.get("summary", "")[:400]} for i, c in enumerate(cands[:25])]
            schema = {"type": "object", "properties": {"id": {"type": "integer"}, "reason": {"type": "string"}},
                      "required": ["id", "reason"], "additionalProperties": False}
            try:
                out = self.llm.json(
                    "You choose business case studies for a reader. Reader goal: " + self.profile["goals"],
                    f"This edition's rotation: {rot['business'] if rot else 'any'} — lesson: {rot['lesson'] if rot else ''}.\n"
                    f"Selection rule: {slot['selection_rule']}\nPrefer small teams and concrete numbers. Pick one.\n"
                    f"{json.dumps(listing, ensure_ascii=False)}", schema)
                if 0 <= out["id"] < len(cands):
                    pick = cands[out["id"]]
            except (LLMUnavailable, KeyError, ValueError) as e:
                self.warn(f"AI selection failed for Startup Story: {e}")
        pick = pick or cands[0]
        try:
            art = fetchers.fetch_article(pick["url"])
        except fetchers.FetchError as e:
            self.warn(f"Could not read the case study page ({e}); using the listing blurb.")
            art = {"text": pick.get("summary", ""), "published_at": None, "title": pick["title"], "image": None, "url": pick["url"]}
        item = self._compose_case(slot, pick, art, rot)
        return {"items": [item], "rotation": rot}

    def _compose_case(self, slot, pick, art, rot):
        base = {"headline": art.get("title") or pick["title"], "publisher": pick.get("publisher") or "Case study", "url": pick["url"],
                "published_at": art.get("published_at"), "rotation_business": rot["business"] if rot else "",
                "lesson": rot["lesson"] if rot else "", "image": art.get("image"),
                "original_minutes": max(1, round(words(art.get("text")) / WPM))}
        if self.llm.available:
            fig = {"type": "object", "properties": {"label": {"type": "string"}, "value": {"type": "string"},
                                                    "kind": {"type": "string", "enum": ["reported", "estimate"]},
                                                    "attribution": {"type": "string"}},
                   "required": ["label", "value", "kind", "attribution"], "additionalProperties": False}
            schema = {"type": "object", "properties": {
                "business_name": {"type": "string"}, "summary": {"type": "string"}, "what_it_sells": {"type": "string"},
                "who_pays": {"type": "string"}, "acquisition": {"type": "string"}, "pricing": {"type": "string"},
                "revenue": {"type": "string"}, "costs": {"type": "string"}, "lesson_takeaway": {"type": "string"},
                "figures": {"type": "array", "items": fig}, "price_per_customer_usd_month": {"type": ["number", "null"]}},
                "required": ["business_name", "summary", "what_it_sells", "who_pays", "acquisition", "pricing", "revenue",
                             "costs", "lesson_takeaway", "figures", "price_per_customer_usd_month"], "additionalProperties": False}
            prompt = (
                f"Write a Startup Story case study (max {slot.get('length_limit_words', 260)} words across all fields) "
                f"from this founder interview. The lesson to draw out: {base['lesson']}.\n"
                "Rules: attribute founder-reported numbers ('the founder says…'). Keep reported figures distinct from "
                "estimates. If costs are not disclosed, say so plainly in `costs`. pricing: what it charges, or 'Not disclosed'. "
                "price_per_customer_usd_month: only if stated or directly derivable, else null.\n\n"
                f"Title: {base['headline']}\nPublished: {base['published_at']}\n\n{(art.get('text') or '')[:14000]}")
            try:
                out = self.llm.json("You are a business journalist who explains unit economics plainly.", prompt, schema, effort="high")
                base.update(out)
                return base
            except (LLMUnavailable, KeyError, ValueError) as e:
                self.warn(f"AI writing failed for Startup Story, used extract: {e}")
        text = art.get("text") or pick.get("summary", "")
        rev = re.search(r"\$[\d.,]+\s?[KkMm]?\s?(?:/\s?mo(?:nth)?|/\s?year| revenue/mo)", pick.get("summary", "") + " " + text)
        base.update({
            "business_name": re.sub(r"^(How|I|We|My|Our)\b.*", "", base["headline"]).strip() or base["headline"],
            "summary": sentences(text, 3),
            "what_it_sells": "", "who_pays": "", "acquisition": "", "pricing": "Not disclosed in the extract",
            "revenue": f"{rev.group(0)} (founder-reported)" if rev else "Not disclosed",
            "costs": "Costs are not disclosed in the case study.",
            "lesson_takeaway": "", "figures": [], "price_per_customer_usd_month": None,
        })
        return base

    # ------------------------------------------------------------------ Do the Maths

    def exercise(self, slot: dict, case: dict | None, usd_inr: float) -> dict:
        if not case:
            return {"items": []}
        target = int(slot.get("target_profit_inr") or 100000)
        ex = None
        if self.llm.available:
            inp = {"type": "object", "properties": {
                "key": {"type": "string"}, "label": {"type": "string"}, "value": {"type": "number"}, "unit": {"type": "string"},
                "kind": {"type": "string", "enum": ["reported", "estimate", "assumption", "goal"]}, "note": {"type": "string"}},
                "required": ["key", "label", "value", "unit", "kind", "note"], "additionalProperties": False}
            step = {"type": "object", "properties": {"key": {"type": "string"}, "label": {"type": "string"},
                                                     "expr": {"type": "string"}, "unit": {"type": "string"}},
                    "required": ["key", "label", "expr", "unit"], "additionalProperties": False}
            schema = {"type": "object", "properties": {
                "question": {"type": "string"}, "inputs": {"type": "array", "items": inp}, "steps": {"type": "array", "items": step},
                "answer_key": {"type": "string"}, "answer_template": {"type": "string"}, "takeaway": {"type": "string"}},
                "required": ["question", "inputs", "steps", "answer_key", "answer_template", "takeaway"], "additionalProperties": False}
            prompt = (
                "Create one practical 'Do the Maths' exercise about this business's economics for this reader. "
                f"Reader goal: {self.profile['goals']}\nDefault question: "
                f"how many customers it would need to make ₹{maths.indian(target)} a month in operating profit — adapt it if "
                "a different question teaches more for this business.\n"
                "Rules: inputs are numbers only. Mark each input reported (from the case study), estimate (derived from "
                "reported figures) or assumption (yours), with a short note saying where it comes from. Convert to rupees "
                f"using an input usd_inr = {usd_inr} (kind assumption, note 'approx. market rate'). Steps are arithmetic "
                "expressions over input and earlier step keys using + - * / ( ) and ceil(), floor(), round(), min(), max(). "
                "Do not compute values yourself; they are computed from the expressions. answer_template is one sentence "
                "using {step_key} placeholders; each placeholder renders with its unit (e.g. {needed} becomes '5 clients'), so don't repeat the unit. Keys are snake_case identifiers.\n\n"
                f"Case study:\n{json.dumps({k: case.get(k) for k in ['business_name','summary','what_it_sells','who_pays','pricing','revenue','costs','figures']}, ensure_ascii=False)}")
            try:
                ex = self.llm.json("You design honest back-of-the-envelope business exercises.", prompt, schema, effort="high")
                maths.compute(ex)
            except (LLMUnavailable, KeyError, ValueError, maths.MathsError, TypeError) as e:
                self.warn(f"AI exercise failed validation, used the template: {e}")
                ex = None
        if ex is None:
            price = case.get("price_per_customer_usd_month")
            ex = maths.fallback_exercise(case.get("business_name") or "this business", price, target, usd_inr,
                                         "reported" if price else "assumption")
            maths.compute(ex)
        ex["answer"] = render_answer(ex)
        ex["business_name"] = case.get("business_name")
        ex["case_url"] = case.get("url")
        return {"items": [ex]}

    # ------------------------------------------------------------------ You asked

    def questions(self, slot: dict) -> dict:
        pending = list(reversed(self.db.list_questions("pending")))[: int(slot.get("item_limit") or 3)]
        items = []
        for q in pending:
            self.step(f"Looking into your note: {q['text'][:60]}")
            query = self._search_query(q["text"])
            found: list[dict] = []
            for src in slot.get("sources", []):
                rec = {"slot": slot["id"], "source": src["name"], "method": src["method"], "url": src.get("url"),
                       "status": "ok", "count": 0, "error": "", "fallback_used": False, "seconds": 0.0}
                t0 = time.time()
                try:
                    if src["method"] == "gnews_search":
                        url = src["url"].replace("{query}", fetchers.quote_plus(query))
                        rec["url"] = url
                        res = fetchers.fetch_gnews(url, limit=8, decode=False)
                        for r in res[:4]:
                            real = fetchers.decode_google_news_url(r["url"])
                            if real:
                                r["url"], r["direct_link"] = real, True
                        found.extend(res[:6])
                    elif src["method"] == "wikipedia":
                        found.extend(fetchers.fetch_wikipedia(query))
                    elif src["method"] == "rss":
                        res = [r for r in fetchers.fetch_rss(src["url"], src["name"]) if similar(r["title"], q["text"]) > 0.1]
                        found.extend(res[:4])
                except (fetchers.FetchError, ValueError, KeyError) as e:
                    rec["status"], rec["error"] = "failed", str(e)[:200]
                rec["count"] = len(found)
                rec["seconds"] = round(time.time() - t0, 1)
                self.diag["sources"].append(rec)
            items.append(self._answer(slot, q, found))
        return {"items": items}

    def _search_query(self, text: str) -> str:
        """Free-text notes can be long; turn them into a short search query."""
        if len(text.split()) <= 10:
            return text
        if self.llm.available:
            try:
                out = self.llm.json("You turn a reader's note into a web search query.",
                                    f"Note: {text}\nReturn the 3 to 8 word search query that best finds what they are curious about.",
                                    {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"],
                                     "additionalProperties": False}, max_tokens=200, effort="low")
                if out.get("query"):
                    return out["query"][:120]
            except Exception as e:  # noqa: BLE001
                self.warn(f"Could not shorten a note into a search: {e}")
        kw = [w for w in re.findall(r"[A-Za-z0-9₹$%'-]+", text) if w.lower() not in STOP and len(w) > 2]
        return " ".join(kw[:8]) or text[:80]

    def _answer(self, slot, q, found):
        item = {"question_id": q["id"], "question": q["text"], "asked_at": q["created_at"], "answer": "", "sources": []}
        docs = [{"id": i, "title": f["title"], "publisher": f["publisher"], "published_at": f.get("published_at"),
                 "url": f["url"], "text": (f.get("text") or f.get("summary") or "")[:1500]} for i, f in enumerate(found[:10])]
        if self.llm.available and docs:
            schema = {"type": "object", "properties": {"answer": {"type": "string"},
                                                       "source_ids": {"type": "array", "items": {"type": "integer"}},
                                                       "settled": {"type": "boolean"}},
                      "required": ["answer", "source_ids", "settled"], "additionalProperties": False}
            try:
                out = self.llm.json(
                    "You answer a curious reader's question briefly and honestly, citing sources.",
                    f"Question: {q['text']}\nAnswer in at most {slot.get('length_limit_words', 90)} words using only these "
                    "sources (headlines alone can be thin; say so if they don't settle it). List the source ids you used.\n"
                    + json.dumps(docs, ensure_ascii=False), schema)
                item["answer"] = out["answer"]
                item["settled"] = out["settled"]
                item["sources"] = [{k: docs[i][k] for k in ("title", "publisher", "url", "published_at")}
                                   for i in out["source_ids"] if 0 <= i < len(docs)][:4]
            except (LLMUnavailable, KeyError, ValueError) as e:
                self.warn(f"AI answer failed for a question: {e}")
        if not item["answer"]:
            wiki = next((d for d in docs if d["publisher"] == "Wikipedia"), None)
            item["answer"] = sentences(wiki["text"], 2) if wiki else "Here is what the news turned up — follow the links."
            item["settled"] = False
            item["sources"] = [{k: d[k] for k in ("title", "publisher", "url", "published_at")} for d in docs[:4]]
        return item

    # ------------------------------------------------------------------ Weekend Deep Dive

    def long_read(self, slot: dict) -> dict:
        raw = self.fetch_slot(slot)
        past_urls = {p["url"] for p in self.past}
        cands = [c for c in raw if c["url"] not in past_urls and urlparse(c["url"]).path.strip("/")]  # skip bare homepage links
        if not cands:
            return {"items": []}
        pick = cands[0]
        if self.llm.available:
            listing = [{"id": i, "title": c["title"], "publisher": c["publisher"], "summary": c.get("summary", "")[:300]}
                       for i, c in enumerate(cands[:40])]
            schema = {"type": "object", "properties": {"id": {"type": "integer"}, "question": {"type": "string"},
                                                       "brief": {"type": "string"}},
                      "required": ["id", "question", "brief"], "additionalProperties": False}
            try:
                out = self.llm.json("You pick one podcast episode for a weekend.",
                                    f"Rule: {slot['selection_rule']}\nReader goal: {self.profile['goals']}\nPick one; give the "
                                    "question it answers and a two-sentence description.\n" + json.dumps(listing, ensure_ascii=False),
                                    schema)
                if 0 <= out["id"] < len(cands):
                    pick = cands[out["id"]]
                    pick["question"], pick["brief"] = out["question"], out["brief"]
            except (LLMUnavailable, KeyError, ValueError) as e:
                self.warn(f"AI selection failed for Weekend Deep Dive: {e}")
        return {"items": [{"headline": pick["title"], "publisher": pick["publisher"], "url": pick["url"],
                           "published_at": pick.get("published_at"), "question": pick.get("question", ""),
                           "brief": pick.get("brief") or sentences(pick.get("summary", ""), 2)}]}


def render_answer(ex: dict) -> str:
    vals = {s["key"]: s["value"] for s in ex.get("steps", [])}
    vals.update({i["key"]: i["value"] for i in ex.get("inputs", [])})
    units = {s["key"]: s.get("unit", "") for s in ex.get("steps", [])}

    def rep(m):
        k = m.group(1)
        return maths.fmt(vals[k], units.get(k, "")) if k in vals else m.group(0)
    return re.sub(r"\{(\w+)\}", rep, ex.get("answer_template", ""))


def usd_inr_rate(diag: dict) -> float:
    try:
        r = fetchers.get("https://open.er-api.com/v6/latest/USD")
        rate = float(r.json()["rates"]["INR"])
        if 50 < rate < 200:
            return round(rate, 2)
    except (fetchers.FetchError, KeyError, ValueError, TypeError) as e:
        diag["validation"].append(f"Exchange rate lookup failed ({e}); used ₹{DEFAULT_USD_INR} per $.")
    return DEFAULT_USD_INR


# ============================================================================ orchestration

def run_pipeline(db: DB, edition_date: str | None = None, new_issue: bool = False, now: datetime | None = None,
                 llm: LLM | None = None) -> dict:
    profile = db.get_profile()
    plan = db.current_plan()
    if not profile or not plan:
        raise RuntimeError("Profile or plan missing — run the seed first.")
    edition_date = edition_date or today_in(profile["timezone"])
    if db.latest_edition_for(edition_date) and not new_issue:
        return {"status": "skipped", "message": "Today's edition is already published; it was left as it is.", "date": edition_date}
    now = now or datetime.now(timezone.utc)
    llm = llm or LLM(profile.get("llm_provider", "auto"))
    run_id = db.start_run(edition_date, plan["id"])
    R = Run(db, profile, plan, edition_date, llm, now)
    try:
        R.step(f"Plan version {plan['id']} · writer: {llm.name}")
        sections = []
        case_item = None
        usd_inr = None
        for slot in plan["slots"]:
            if not slot.get("enabled", True) or not runs_on(slot, R.day):
                continue
            kind = slot.get("kind")
            try:
                if kind == "news":
                    res = R.news(slot)
                elif kind == "explainer":
                    res = R.explainer(slot)
                elif kind == "case_study":
                    res = R.case_study(slot)
                    case_item = res["items"][0] if res["items"] else None
                elif kind == "exercise":
                    usd_inr = usd_inr or usd_inr_rate(R.diag)
                    res = R.exercise(slot, case_item, usd_inr)
                elif kind == "questions":
                    res = R.questions(slot)
                elif kind == "long_read":
                    res = R.long_read(slot)
                else:
                    R.warn(f"Unknown slot kind {kind!r}; skipped.")
                    continue
            except Exception as e:  # noqa: BLE001 - a broken slot should shorten the issue, not kill it
                R.warn(f"{slot['slot']} failed and was left out: {e}")
                log.exception("slot failed")
                continue
            sections.append({"id": slot["id"], "kind": kind, "section": slot["section"], "slot": slot["slot"],
                             "purpose": slot.get("purpose", ""), "items": res.get("items", []),
                             "rotation": res.get("rotation")})
        # The exercise must follow its case study even if the reader reordered slots.
        content = compose_edition(R, sections)
        validate(R, content)
        sources = edition_source_rows(content)
        if not any(s["items"] for s in content["sections"]):
            raise RuntimeError("Nothing could be fetched; no edition published.")
        res = db.publish_edition(edition_date, content, plan["id"], content["reading"]["total_minutes"], sources, new_issue=new_issue)
        if res is None:
            db.finish_run(run_id, "skipped", "An issue for this date already exists; left intact.", R.diag)
            return {"status": "skipped", "message": "An issue for this date already exists.", "date": edition_date}
        eid, issue_no = res
        for s in content["sections"]:
            if s["kind"] == "questions":
                for it in s["items"]:
                    db.set_question_status(it["question_id"], "answered", eid)
        R.step(f"Published issue {issue_no} for {edition_date}")
        db.finish_run(run_id, "published", f"Published issue {issue_no}", R.diag, eid)
        return {"status": "published", "date": edition_date, "issue_no": issue_no, "edition_id": eid,
                "warnings": R.diag["validation"]}
    except Exception as e:  # noqa: BLE001
        R.diag["error"] = traceback.format_exc()[-3000:]
        db.finish_run(run_id, "failed", str(e)[:500], R.diag)
        return {"status": "failed", "message": str(e), "date": edition_date}


def compose_edition(R: Run, sections: list[dict]) -> dict:
    # Put the exercise directly after the case study.
    ex = next((s for s in sections if s["kind"] == "exercise"), None)
    cs = next((s for s in sections if s["kind"] == "case_study"), None)
    if ex and cs:
        sections.remove(ex)
        sections.insert(sections.index(cs) + 1, ex)
    lead = choose_lead(sections)
    content = {
        "title": "The Morning Paper",
        "date": R.date,
        "weekday": R.day.strftime("%A"),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "plan_version": R.plan["id"],
        "writer": R.llm.name,
        "lead": lead,
        "sections": sections,
    }
    content["reading"] = reading_time(content)
    return content


def choose_lead(sections: list[dict]) -> dict:
    news = next((s for s in sections if s["kind"] == "news" and s["items"]), None)
    top = max(news["items"], key=lambda i: i.get("consequence", 0)) if news else None
    if top and top.get("consequence", 0) >= 8:
        return {"section_id": news["id"], "index": news["items"].index(top), "headline": top["headline"],
                "summary": top["brief"], "kind": "news", "why": "A consequential development leads today."}
    for kind in ("explainer", "case_study"):
        s = next((s for s in sections if s["kind"] == kind and s["items"]), None)
        if s:
            it = s["items"][0]
            return {"section_id": s["id"], "index": 0, "headline": it.get("headline"),
                    "summary": it.get("brief") or it.get("summary", ""), "kind": kind,
                    "why": "An ordinary news day, so Business leads."}
    if top:
        return {"section_id": news["id"], "index": news["items"].index(top), "headline": top["headline"],
                "summary": top["brief"], "kind": "news", "why": ""}
    return {}


def reading_time(content: dict) -> dict:
    front = business = 0
    originals = 0
    for s in content["sections"]:
        for it in s["items"]:
            w = words(*[v for k, v in it.items() if isinstance(v, str) and k not in ("url", "published_at", "image")])
            if s["kind"] in ("news", "questions"):
                front += w
            else:
                business += w
            if s["kind"] in ("explainer", "case_study"):
                originals += it.get("original_minutes", 0) or 0
    front_min = max(1, math.ceil(front / WPM))
    business_min = math.ceil(business / WPM) + originals + (3 if any(s["kind"] == "exercise" and s["items"] for s in content["sections"]) else 0)
    return {"front_minutes": front_min, "business_minutes": business_min, "total_minutes": front_min + business_min}


def validate(R: Run, content: dict) -> None:
    """Check links, dates, arithmetic, duplication and reading length. Mutates content to drop bad items."""
    report = []
    urls = []
    for s in content["sections"]:
        for it in s["items"]:
            if it.get("url"):
                urls.append((s, it, it["url"]))
            for src in it.get("sources", []):
                urls.append((s, src, src["url"]))
    # Links
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda t: fetchers.check_link(t[2]), urls))
    for (s, it, url), (status, detail) in zip(urls, results):
        it["link_status"] = status
        if status == "broken":
            report.append(f"Broken link in {s['slot']}: {url} ({detail})")
            if s["kind"] == "news" and it in s["items"]:
                s["items"].remove(it)
                report.append(f"Dropped “{it.get('headline')}” because its link is broken.")
        elif status == "blocked":
            report.append(f"Could not verify {url}: {detail}. Kept.")
    # Dates
    for s in content["sections"]:
        for it in s["items"]:
            if s["kind"] in ("news", "explainer", "case_study", "long_read") and not it.get("published_at"):
                report.append(f"No publication date found for “{it.get('headline')}”.")
    # Arithmetic
    for s in content["sections"]:
        if s["kind"] == "exercise":
            for ex in s["items"]:
                try:
                    maths.compute(ex)
                    if ex.get("answer_value") is None or ex["answer_value"] <= 0:
                        report.append("Maths answer is not a positive number — check the inputs.")
                except maths.MathsError as e:
                    report.append(f"Maths failed to recompute: {e}")
                    s["items"] = []
    # Duplication within the edition
    seen = {}
    for s in content["sections"]:
        for it in list(s["items"]):
            u = it.get("url")
            if not u:
                continue
            if u in seen:
                s["items"].remove(it)
                report.append(f"Removed a duplicate of “{it.get('headline')}”.")
            seen[u] = s["id"]
    # Lead still valid?
    lead = content.get("lead") or {}
    sec = next((s for s in content["sections"] if s["id"] == lead.get("section_id")), None)
    if lead and (not sec or lead.get("index", 0) >= len(sec["items"]) or sec["items"][lead["index"]].get("headline") != lead.get("headline")):
        content["lead"] = choose_lead(content["sections"])
    # Reading length
    content["reading"] = reading_time(content)
    fp = R.profile.get("front_page_minutes", 5)
    bm = R.profile.get("business_minutes", 20)
    if content["reading"]["front_minutes"] > fp + 2:
        report.append(f"Front page runs {content['reading']['front_minutes']} min (target {fp}).")
    if content["reading"]["business_minutes"] > bm + 5:
        report.append(f"Business runs about {content['reading']['business_minutes']} min including originals (target {bm}).")
    content["validation"] = report
    R.diag["validation"].extend(report)


def edition_source_rows(content: dict) -> list[dict]:
    rows = []
    for s in content["sections"]:
        for it in s["items"]:
            if it.get("url") and s["kind"] != "exercise":
                key = ""
                if s["kind"] == "case_study" and it.get("rotation_business"):
                    key = f"rotation:{it['rotation_business']}"
                rows.append({"slot_id": s["id"], "title": it.get("source_title") or it.get("headline") or "",
                             "publisher": it.get("publisher", ""), "published_at": it.get("published_at"),
                             "url": it["url"], "story_key": key})
            for src in it.get("sources", []):
                rows.append({"slot_id": s["id"], "title": src.get("title", ""), "publisher": src.get("publisher", ""),
                             "published_at": src.get("published_at"), "url": src["url"], "story_key": ""})
    return rows
