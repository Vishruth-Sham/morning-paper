"""New section kinds (tasks, weather, markets, prompt) and the "Your day" / "More for you" layout."""
from paper import extras, fetchers, pipeline

from tests.test_pipeline import app, run  # noqa: F401 - reuse the stubbed app fixture


class FakeResp:
    def __init__(self, data, status=200):
        self._d, self.status_code = data, status

    def json(self):
        return self._d


def add_extra_slots(db):
    base = {"sources": [], "selection_rule": "", "frequency": "daily", "freshness_hours": 24, "display_format": "brief",
            "item_limit": 3, "length_limit_words": 40, "enabled": True, "edited_fields": []}
    plan = db.current_plan()
    slots = plan["slots"] + [
        {**base, "id": "day-weather", "kind": "weather", "section": "Weather", "slot": "Weather", "purpose": "",
         "placement": "top", "places": ["Sampletown"]},
        {**base, "id": "day-markets", "kind": "markets", "section": "Markets", "slot": "Markets", "purpose": "",
         "placement": "top", "symbols": [{"label": "Index", "symbol": "^IDX"}], "usd_inr": False},
        {**base, "id": "idea", "kind": "concept", "section": "Business", "slot": "Concept", "purpose": "",
         "concepts": [{"name": "Contribution margin"}]},
        {**base, "id": "nudge", "kind": "prompt", "section": "Nudge", "slot": "Nudge", "purpose": "", "placement": "more",
         "ideas": ["Call a friend."]},
        {**base, "id": "hobby", "kind": "news", "section": "Hobby", "slot": "Hobby", "purpose": "", "placement": "more",
         "sources": [{"name": "Hobbyist", "method": "rss", "url": "https://wire.example.com/rss", "fallback_method": "", "fallback_url": ""}]},
    ]
    db.save_plan(slots, "test", "manual", db.get_profile())


def fake_get(url, params=None, **kw):
    if "geocoding" in url:
        return FakeResp({"results": [{"name": "Sampletown", "latitude": 1.0, "longitude": 2.0}]})
    if "forecast" in url:
        return FakeResp({"daily": {"weather_code": [1], "temperature_2m_max": [30.4], "temperature_2m_min": [21.2],
                                   "precipitation_probability_max": [10], "wind_speed_10m_max": [8], "sunrise": ["2026-11-21T06:01"]},
                         "hourly": {"time": ["2026-11-21T06:00"], "temperature_2m": [22.0]}})
    if "finance" in url:
        return FakeResp({"chart": {"result": [{"indicators": {"quote": [{"close": [100.0, 102.0]}]}}]}})
    raise fetchers.FetchError("unexpected " + url)


def test_extra_sections_render(app, monkeypatch):
    db = app.config["DB"]
    add_extra_slots(db)
    monkeypatch.setattr(fetchers, "get", fake_get)
    orig = pipeline.Run._fetch_by_method

    def fetch(self, m, u, src):
        if src["name"] == "Hobbyist":
            return [fetchers.candidate(title="New trail opens in the hills", publisher="Hobbyist",
                                       url="https://hobby.example.com/trail", published_at="2026-11-20T20:00:00+00:00")]
        return orig(self, m, u, src)
    monkeypatch.setattr(pipeline.Run, "_fetch_by_method", fetch)
    res = run(app)
    assert res["status"] == "published", res
    ed = db.get_edition("2026-11-21", 1)
    secs = {s["id"]: s for s in ed["content"]["sections"]}
    w = secs["day-weather"]["items"][0]
    assert w["place"] == "Sampletown" and w["early_run"] == 22
    q = secs["day-markets"]["items"][0]
    assert q["value"] == 102.0 and q["change_pct"] == 2.0
    assert secs["nudge"]["items"][0]["text"] == "Call a friend."
    con = secs["idea"]["items"][0]
    assert con["concept"] == "Contribution margin" and con["case_url"] == "https://example.com/a"
    assert con["explanation"].startswith("Topic is a thing.")
    assert secs["hobby"]["placement"] == "more" and secs["hobby"]["items"]
    # a "more" section never takes the lead
    assert ed["content"]["lead"]["section_id"] == "news"
    html = app.test_client().get("/edition/2026-11-21/1").get_data(as_text=True)
    assert "Concept of the day" in html and 'class="yourday"' in html and "Sampletown" in html and "+2.00%" in html
    assert "More for you" in html and "Call a friend." in html and "New trail opens" in html


def test_tasks_without_token_is_a_note(app):
    class R:
        day = None
        def warn(self, m): pass
    out = extras.tasks(R(), {"slot": "Tasks", "env_file": "/nonexistent/.env"})
    assert out["items"][0]["kind"] == "note"
