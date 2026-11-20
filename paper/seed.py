"""Starting values: a small default profile and plan. Saving your answers in the app redrafts the
plan from them. Your own starting values can live in paper/seed_local.py (not committed)."""

SEED_PROFILE = {
    "interests": "How businesses make money.",
    "goals": "Understand the world a little better each morning.",
    "always_cover": "Important world news: what changed and who it affects.",
    "reading_preference": "A front page I can skim in a few minutes. Short descriptions and direct links to the originals.",
    "front_page_minutes": 5,
    "business_minutes": 20,
    "timezone": "UTC",
    "preferred_sources": "",
    "llm_provider": "auto",
}

ROTATION: list[dict] = []


def src(name, method, url, fallback_method="", fallback_url="", **extra):
    d = {"name": name, "method": method, "url": url, "fallback_method": fallback_method, "fallback_url": fallback_url}
    d.update(extra)
    return d


SEED_SLOTS = [
    {
        "id": "news",
        "kind": "news",
        "section": "News",
        "slot": "Top stories",
        "purpose": "What changed in the world today, and who it affects.",
        "sources": [src("Google News — top stories", "rss", "https://news.google.com/rss?hl=en&gl=US&ceid=US:en")],
        "selection_rule": "Rank by consequence for many people. Skip crime, celebrity and opinion. Merge duplicate coverage.",
        "frequency": "daily",
        "freshness_hours": 36,
        "display_format": "brief",
        "item_limit": 3,
        "length_limit_words": 60,
        "enabled": True,
        "edited_fields": [],
    },
    {
        "id": "you-asked",
        "kind": "questions",
        "section": "You asked",
        "slot": "From your notebook",
        "purpose": "Short, sourced answers to what you wrote in your notebook.",
        "sources": [
            src("Google News search", "gnews_search", "https://news.google.com/rss/search?q={query}&hl=en&gl=US&ceid=US:en"),
            src("Wikipedia", "wikipedia", "https://en.wikipedia.org/w/api.php"),
        ],
        "selection_rule": "Answer up to three notes, oldest first. Ground every answer in the fetched sources and link them.",
        "frequency": "daily",
        "freshness_hours": 0,
        "display_format": "brief",
        "item_limit": 3,
        "length_limit_words": 90,
        "enabled": True,
        "edited_fields": [],
    },
]

# Your own starting values, if you have them.
try:
    from . import seed_local as _local
    SEED_PROFILE = getattr(_local, "SEED_PROFILE", SEED_PROFILE)
    ROTATION = getattr(_local, "ROTATION", ROTATION)
    SEED_SLOTS = getattr(_local, "SEED_SLOTS", SEED_SLOTS)
except ImportError:
    pass

EDITABLE_FIELDS = ["section", "slot", "purpose", "sources", "selection_rule", "frequency", "freshness_hours",
                   "display_format", "item_limit", "length_limit_words", "enabled"]
