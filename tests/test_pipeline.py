from datetime import datetime, timedelta, timezone

import pytest

from paper import fetchers, pipeline
from paper.app import create_app
from paper.llm import LLM

NOW = datetime(2026, 11, 21, 2, 0, tzinfo=timezone.utc)


def iso(hours_ago):
    return (NOW - timedelta(hours=hours_ago)).isoformat()


def fake_items(method, url, src):
    name = src["name"]
    if name == "Wire":
        return [fetchers.candidate(title="Parliament passes new tariff bill affecting exporters", publisher="Wire",
                                   url="https://wire.example.com/world/tariff-bill-2026-11-20", published_at=iso(5),
                                   summary="Parliament passed a bill. It changes tariffs for exporters."),
                fetchers.candidate(title="Actor attends film festival", publisher="Wire",
                                   url="https://wire.example.com/lifestyle/x", published_at=iso(3))]
    if name == "Daily":
        return [fetchers.candidate(title="Central bank holds interest rate steady as inflation eases", publisher="Daily",
                                   url="https://daily.example.com/business/rbi-rate/article1.ece", published_at=iso(8),
                                   summary="The central bank kept rates unchanged. Inflation eased to 3%."),
                fetchers.candidate(title="Old story from last week about an election", publisher="Daily",
                                   url="https://daily.example.com/old", published_at=iso(200))]
    if name == "Explainers":
        return [fetchers.candidate(title="Why tyre makers' margins are shrinking", publisher="Explainers",
                                   url="https://explainers.example.com/markets/tyres/", published_at=iso(20),
                                   text="Rubber prices rose 30%. Tyre makers cannot pass costs on quickly. Margins fell."),
                fetchers.candidate(title="Nvidia's crisis, tyres, and more...", publisher="Explainers",
                                   url="https://explainers.example.com/archive/roundup/", published_at=iso(30))]
    if name == "Magazine":
        raise fetchers.FetchError("HTTP 503")
    if name.startswith("Case studies"):
        return [fetchers.candidate(title="How We Started A $8K/Month Bookkeeping Service", publisher="Case studies",
                                   url="https://cases.example.com/stories/bookkeeping-8k",
                                   summary="How much money it makes: $96K/year. Bookkeeping for founders."),
                fetchers.candidate(title="How I Built A $5K/Month Payroll Service", publisher="Case studies",
                                   url="https://cases.example.com/stories/payroll-5k",
                                   summary="How much money it makes: $60K/year. Payroll for small firms.")]
    return []


def fake_article(url):
    return {"url": url, "title": "A page", "published_at": "2020-07-31T00:00:00+00:00", "description": "Desc.",
            "image": None, "text": "We charge $400 per month. We have 20 clients. Costs are mostly staff."}


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline.Run, "_fetch_by_method", lambda self, m, u, s: fake_items(m, u, s))
    monkeypatch.setattr(fetchers, "fetch_article", fake_article)
    monkeypatch.setattr(fetchers, "fetch_gnews", lambda url, publisher_hint="", limit=30, decode=True: [
        fetchers.candidate(title="Explainer on the question topic", publisher="Example News", url="https://example.com/a",
                           published_at=iso(10))])
    monkeypatch.setattr(fetchers, "fetch_wikipedia", lambda q, limit=2: [
        fetchers.candidate(title="Topic", publisher="Wikipedia", url="https://en.wikipedia.org/wiki/Topic",
                           text="Topic is a thing. It has a history.", via="wikipedia")])
    a = create_app(str(tmp_path / "paper.db"))
    publish_first_edition(a.config["DB"])
    return a


def publish_first_edition(db):
    """A small hand-made first edition dated the day before the runs below."""
    item = {"headline": "Budget session opens", "brief": "Parliament met. Bills were listed.", "who_it_affects": "Taxpayers",
            "publisher": "Daily", "published_at": "2026-11-20T03:00:00+00:00", "url": "https://daily.example.com/news/budget-session/",
            "direct_link": True, "also": [], "consequence": 7, "is_update": False}
    content = {"title": "The Morning Paper", "weekday": "Friday", "generated_at": "2026-11-20T03:00:00+00:00", "writer": "test",
               "lead": {"section_id": "news", "index": 0, "headline": item["headline"], "summary": item["brief"], "kind": "news"},
               "sections": [{"id": "news", "kind": "news", "section": "World", "slot": "Top developments",
                             "purpose": "", "items": [item]}],
               "validation": [], "reading": {"front_minutes": 1, "business_minutes": 0, "total_minutes": 1}}
    plan = db.current_plan()
    db.publish_edition("2026-11-20", {**content, "plan_version": plan["id"]}, plan["id"], 1, [])


def run(app, **kw):
    return pipeline.run_pipeline(app.config["DB"], edition_date="2026-11-21", now=NOW, llm=LLM("none"), **kw)


def test_first_edition_present(app):
    db = app.config["DB"]
    eds = db.list_editions()
    assert len(eds) == 1 and eds[0]["date"] == "2026-11-20"


def test_pipeline_publishes_and_is_idempotent(app):
    db = app.config["DB"]
    db.add_question("Why do states set fuel taxes?")
    res = run(app)
    assert res["status"] == "published", res
    ed = db.get_edition("2026-11-21", 1)
    secs = {s["id"]: s for s in ed["content"]["sections"]}
    titles = [i["headline"] for i in secs["news"]["items"]]
    assert "Actor attends film festival" not in titles            # trivial filtered
    assert "Old story from last week about an election" not in titles  # stale filtered
    assert secs["explainer"]["items"][0]["url"] == "https://explainers.example.com/markets/tyres/"  # round-up skipped
    case = secs["case"]["items"][0]
    assert case["published_at"].startswith("2020-07-31")
    ex = secs["maths"]["items"][0]
    assert ex["answer_value"] > 0
    assert secs["you-asked"]["items"][0]["question"].startswith("Why do states")
    assert db.list_questions("pending") == []
    # diagnostics record the failed Magazine fetch
    run_row = db.list_runs(1)[0]
    mc = [s for s in run_row["diagnostics"]["sources"] if s["source"] == "Magazine"][0]
    assert mc["status"] == "failed"
    # repeated run leaves the issue intact
    again = run(app)
    assert again["status"] == "skipped"
    assert db.get_edition("2026-11-21", 1)["content"] == ed["content"]


def test_failed_run_leaves_existing_issue(app, monkeypatch):
    db = app.config["DB"]
    assert run(app)["status"] == "published"
    before = db.get_edition("2026-11-21", 1)
    monkeypatch.setattr(pipeline, "compose_edition", lambda R, s: (_ for _ in ()).throw(RuntimeError("boom")))
    res = run(app, new_issue=True)
    assert res["status"] == "failed"
    assert db.get_edition("2026-11-21", 1) == before
    assert db.get_edition("2026-11-21", 2) is None


def test_v1_acceptance_flow(app, tmp_path):
    """Read the seed edition, save a business, edit a rule, generate a subsequent issue with the
    revised plan, and reopen both issues after a restart."""
    c = app.test_client()
    assert c.get("/edition/2026-11-20/1").status_code == 200
    r = c.post("/notebook/new", data={"name": "Test Co", "revenue_model": "Monthly bundle", "estimated_economics": "$8K MRR",
                                      "acquisition_strategy": "Content + referrals", "copy_this": "Referral bonus",
                                      "source_url": "https://cases.example.com/stories/x", "edition_id": "1",
                                      "origin": "edition"}, follow_redirects=True)
    assert b"Test Co" in r.data
    db = app.config["DB"]
    plan = db.current_plan()
    slot = next(s for s in plan["slots"] if s["id"] == "news")
    from paper.planner import sources_to_text
    form = {"section": slot["section"], "slot": slot["slot"], "purpose": slot["purpose"],
            "sources": sources_to_text(slot["sources"]), "selection_rule": "Only economy and policy stories.",
            "frequency": "daily", "freshness_hours": "36", "display_format": "brief", "item_limit": "1",
            "length_limit_words": "60", "enabled": "on"}
    c.post("/edit/plan/slot/news", data=form)
    new_plan = db.current_plan()
    assert new_plan["id"] == plan["id"] + 1
    res = run(app)
    assert res["status"] == "published"
    ed2 = db.get_edition("2026-11-21", 1)
    assert ed2["plan_version"] == new_plan["id"]
    assert len(next(s for s in ed2["content"]["sections"] if s["id"] == "news")["items"]) == 1
    # restart: a fresh app on the same database
    app2 = create_app(app.config["DB"].path)
    c2 = app2.test_client()
    assert c2.get("/edition/2026-11-20/1").status_code == 200
    assert c2.get("/edition/2026-11-21/1").status_code == 200
    assert b"Test Co" in c2.get("/notebook/1/edit").data
    c2.post("/ask", data={"question": "Why do gyms sell yearly memberships?"})
    assert b"Why do gyms sell yearly memberships?" in c2.get("/notebook").data
    arch = c2.get("/archive").data
    assert b"2026-11-20" in arch and b"2026-11-21" in arch
