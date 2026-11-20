"""Stage A: turn the reader profile into an editable curation plan, and propose revisions
when the profile changes. Manual edits are tracked per field and are never overwritten
without the reader ticking that change."""
from __future__ import annotations

import copy
import json

from .seed import EDITABLE_FIELDS, SEED_SLOTS
from .llm import LLM, LLMUnavailable

KINDS = ["news", "explainer", "case_study", "exercise", "questions", "long_read", "tasks", "weather", "markets", "prompt", "concept"]
METHODS = ["rss", "gnews", "gnews_search", "page", "wikipedia", "featured"]

SOURCE_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "method": {"type": "string", "enum": METHODS},
        "url": {"type": "string"},
        "fallback_method": {"type": "string"},
        "fallback_url": {"type": "string"},
    },
    "required": ["name", "method", "url", "fallback_method", "fallback_url"],
    "additionalProperties": False,
}

SLOT_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "kind": {"type": "string", "enum": KINDS},
        "section": {"type": "string"},
        "slot": {"type": "string"},
        "purpose": {"type": "string"},
        "sources": {"type": "array", "items": SOURCE_SCHEMA},
        "selection_rule": {"type": "string"},
        "frequency": {"type": "string", "enum": ["daily", "weekdays", "weekends", "weekly"]},
        "freshness_hours": {"type": "integer"},
        "display_format": {"type": "string", "enum": ["brief", "case_study", "exercise", "long_read"]},
        "item_limit": {"type": "integer"},
        "length_limit_words": {"type": "integer"},
        "enabled": {"type": "boolean"},
        "change_note": {"type": "string"},
    },
    "required": ["id", "kind", "section", "slot", "purpose", "sources", "selection_rule", "frequency",
                 "freshness_hours", "display_format", "item_limit", "length_limit_words", "enabled", "change_note"],
    "additionalProperties": False,
}

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "rationale": {"type": "string"},
        "slots": {"type": "array", "items": SLOT_SCHEMA},
    },
    "required": ["rationale", "slots"],
    "additionalProperties": False,
}

SYSTEM = """You are the editor of a one-person morning newspaper. You translate a reader's profile into a
curation plan: a list of recurring slots, each saying where stories come from, how they are fetched, how
they are chosen and how they are shown. The paper must be calm and finite: a front page skimmable in a
few minutes plus optional deeper sections that fit the reader's stated reading time.

Rules:
- Standing coverage ("always cover") is a separate section judged on consequence, independent of interests.
- Each interest gets a personal section whose angle and format follow the reader's goal.
- Only use fetch methods from: rss (an RSS/Atom feed URL), gnews (a Google News RSS URL restricted with site:),
  gnews_search (Google News search URL with {query}), page (an HTML listing page to extract links from),
  wikipedia, featured (reuses a business featured elsewhere in the edition). Give a fallback where one exists.
- Only name feed URLs you are confident exist. Prefer RSS, then Google News, then page extraction.
- Keep slot ids stable for slots that continue to exist. New slots get a new kebab-case id.
- Keep the "questions" slot (answers to the reader's own questions) unless the reader asks to drop it.
- Keep tasks, weather, markets, prompt and concept slots (they have no sources; their settings live outside this
  schema and are kept automatically) unless the reader asks to drop them.
- change_note says in one sentence what changed versus the current plan and why ("" if unchanged)."""


def initial_slots() -> list[dict]:
    return copy.deepcopy(SEED_SLOTS)


def generate_plan(profile: dict, current: list[dict] | None, llm: LLM) -> tuple[list[dict], str]:
    """Ask the model for a plan for this profile. Returns (slots, rationale)."""
    if not llm.available:
        raise LLMUnavailable("No language model configured. Set ANTHROPIC_API_KEY or sign in to the claude CLI.")
    base = current or initial_slots()
    prompt = (
        "Reader profile:\n" + json.dumps(_profile_for_prompt(profile), indent=2, ensure_ascii=False) +
        "\n\nCurrent plan (keep what still fits; these sources and fetch methods were verified to work):\n" +
        json.dumps([_slot_for_prompt(s) for s in base], indent=2, ensure_ascii=False) +
        "\n\nReturn the full revised plan."
    )
    out = llm.json(SYSTEM, prompt, PLAN_SCHEMA, effort="high")
    slots = []
    for s in out["slots"]:
        old = next((o for o in base if o["id"] == s["id"]), None)
        merged = {**(old or {}), **{k: v for k, v in s.items()}}
        merged.setdefault("edited_fields", [])
        if old and "rotation" in old:
            merged["rotation"] = old["rotation"]
        slots.append(merged)
    return slots, out.get("rationale", "")


def diff_plans(current: list[dict], proposed: list[dict]) -> list[dict]:
    """List of changes: {slot_id, slot_label, field, old, new, kind, manually_edited, note}."""
    changes = []
    cur_by_id = {s["id"]: s for s in current}
    prop_by_id = {s["id"]: s for s in proposed}
    for s in proposed:
        old = cur_by_id.get(s["id"])
        label = f"{s.get('section')} · {s.get('slot')}"
        if not old:
            changes.append({"slot_id": s["id"], "slot_label": label, "field": "*", "old": None, "new": s,
                            "kind": "add", "manually_edited": False, "note": s.get("change_note", "")})
            continue
        for f in EDITABLE_FIELDS:
            if _norm(old.get(f)) != _norm(s.get(f)):
                changes.append({"slot_id": s["id"], "slot_label": label, "field": f, "old": old.get(f), "new": s.get(f),
                                "kind": "edit", "manually_edited": f in old.get("edited_fields", []),
                                "note": s.get("change_note", "")})
    for s in current:
        if s["id"] not in prop_by_id:
            changes.append({"slot_id": s["id"], "slot_label": f"{s.get('section')} · {s.get('slot')}", "field": "*",
                            "old": s, "new": None, "kind": "remove", "manually_edited": bool(s.get("edited_fields")),
                            "note": "Not in the proposed plan"})
    return changes


def apply_changes(current: list[dict], proposed: list[dict], accepted: list[int]) -> list[dict]:
    """Apply the accepted change indices (from diff_plans) to the current plan."""
    changes = diff_plans(current, proposed)
    slots = copy.deepcopy(current)
    by_id = {s["id"]: s for s in slots}
    for i in accepted:
        if i < 0 or i >= len(changes):
            continue
        c = changes[i]
        if c["kind"] == "add":
            new = copy.deepcopy(c["new"])
            new.setdefault("edited_fields", [])
            slots.append(new)
            by_id[new["id"]] = new
        elif c["kind"] == "remove":
            slots = [s for s in slots if s["id"] != c["slot_id"]]
            by_id.pop(c["slot_id"], None)
        else:
            s = by_id.get(c["slot_id"])
            if s is not None:
                s[c["field"]] = copy.deepcopy(c["new"])
                # Accepting an AI change to a field means it is no longer a manual override.
                s["edited_fields"] = [f for f in s.get("edited_fields", []) if f != c["field"]]
    # Keep proposed order for slots that exist in both.
    order = {s["id"]: i for i, s in enumerate(proposed)}
    slots.sort(key=lambda s: order.get(s["id"], len(order)))
    for s in slots:
        s.pop("change_note", None)
    return slots


def edit_slot(slots: list[dict], slot_id: str, updates: dict) -> list[dict]:
    """Manual edit from the plan table. Marks changed fields as reader-edited."""
    slots = copy.deepcopy(slots)
    for s in slots:
        if s["id"] != slot_id:
            continue
        for f, v in updates.items():
            if f not in EDITABLE_FIELDS:
                continue
            if _norm(s.get(f)) != _norm(v):
                s[f] = v
                if f not in s.setdefault("edited_fields", []):
                    s["edited_fields"].append(f)
    return slots


def move_slot(slots: list[dict], slot_id: str, delta: int) -> list[dict]:
    slots = copy.deepcopy(slots)
    idx = next((i for i, s in enumerate(slots) if s["id"] == slot_id), None)
    if idx is None:
        return slots
    j = max(0, min(len(slots) - 1, idx + delta))
    slots.insert(j, slots.pop(idx))
    return slots


def parse_sources(text: str) -> list[dict]:
    """One source per line: Name | method | URL | fallback method | fallback URL."""
    out = []
    for line in text.splitlines():
        if not line.strip():
            continue
        raw = line.split("|")
        pidx = next((i for i, p in enumerate(raw) if p.strip().startswith("pattern=")), None)
        parts = [p.strip() for p in (raw[:pidx] if pidx is not None else raw)]
        parts += [""] * (5 - len(parts))
        if pidx is not None:
            parts = parts[:5] + ["|".join(raw[pidx:]).strip()]
        name, method, url, fm, fu = parts[:5]
        if method not in METHODS:
            raise ValueError(f"Unknown fetch method {method!r} for {name!r}. Use one of: {', '.join(METHODS)}")
        out.append({"name": name, "method": method, "url": url, "fallback_method": fm, "fallback_url": fu,
                    **_extra_from(parts[5:])})
    return out


def sources_to_text(sources: list[dict]) -> str:
    lines = []
    for s in sources:
        parts = [s.get("name", ""), s.get("method", ""), s.get("url", ""), s.get("fallback_method", ""),
                 s.get("fallback_url", "")]
        if s.get("link_pattern"):
            parts.append("pattern=" + s["link_pattern"])
        lines.append(" | ".join(parts).rstrip(" |"))
    return "\n".join(lines)


def _extra_from(parts):
    extra = {}
    for p in parts:
        if p.startswith("pattern="):
            extra["link_pattern"] = p[len("pattern="):]
    return extra


def _norm(v):
    return json.dumps(v, sort_keys=True, ensure_ascii=False)


def _profile_for_prompt(p: dict) -> dict:
    return {
        "What are you interested in?": p["interests"],
        "What are you trying to understand or achieve?": p["goals"],
        "What should your paper always cover?": p["always_cover"],
        "Reading preference": p["reading_preference"],
        "Front page minutes": p["front_page_minutes"],
        "Business/personal section minutes": p["business_minutes"],
        "Preferred sources": p["preferred_sources"],
    }


def _slot_for_prompt(s: dict) -> dict:
    return {k: s.get(k) for k in ["id", "kind", *EDITABLE_FIELDS]}
