"""A neutral plan used by the tests, covering every kind of slot."""
from paper.seed import src

ROTATION = [{"business": "Service business", "lesson": "How a small team delivers a recurring service",
             "url": "https://cases.example.com/topics/services", "keywords": ["service", "bookkeeping", "payroll"]}]

PROFILE = {
    "interests": "How businesses make money.", "goals": "Learn how small businesses work.",
    "always_cover": "Important world news.", "reading_preference": "Short.", "front_page_minutes": 5,
    "business_minutes": 20, "timezone": "UTC", "preferred_sources": "", "llm_provider": "auto",
}


def slot(id, kind, section, name, sources, **kw):
    d = {"id": id, "kind": kind, "section": section, "slot": name, "purpose": f"{name} purpose", "sources": sources,
         "selection_rule": f"{name} rule", "frequency": "daily", "freshness_hours": 0, "display_format": "brief",
         "item_limit": 1, "length_limit_words": 60, "enabled": True, "edited_fields": []}
    d.update(kw)
    return d


SLOTS = [
    slot("news", "news", "World", "Top stories",
         [src("Wire", "gnews", "https://news.example.com/rss/search?q=site:wire.example.com", "page", "https://wire.example.com/world/"),
          src("Daily", "rss", "https://daily.example.com/news/feed.rss")],
         freshness_hours=36, item_limit=3),
    slot("explainer", "explainer", "Business", "Explainer",
         [src("Explainers", "rss", "https://explainers.example.com/rss/"),
          src("Magazine", "page", "https://magazine.example.com/", link_pattern=r"^https://magazine\.example\.com/(a|b)/[a-z0-9-]+$")],
         freshness_hours=72),
    slot("case", "case_study", "Business", "Case study",
         [src("Case studies — rotation topic", "page", "rotation", "page", "https://cases.example.com/all/",
              link_pattern=r"^https://cases\.example\.com/(stories/[a-z0-9-]+|[a-z0-9-]+)$")],
         display_format="case_study", rotation=ROTATION),
    slot("maths", "exercise", "Business", "Do the Maths", [src("The featured business", "featured", "")],
         display_format="exercise", target_profit_inr=100000),
    slot("you-asked", "questions", "You asked", "From your notebook",
         [src("News search", "gnews_search", "https://news.example.com/rss/search?q={query}"),
          src("Wikipedia", "wikipedia", "https://en.wikipedia.org/w/api.php")], item_limit=3),
    slot("deep", "long_read", "Business", "Weekend listen", [src("Podcast", "rss", "https://podcast.example.com/feed")],
         frequency="weekends", display_format="long_read"),
]
