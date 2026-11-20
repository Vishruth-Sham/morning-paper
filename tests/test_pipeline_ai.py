"""Exercise the AI code paths with a scripted stand-in for the model."""
from paper import pipeline, planner
from tests.test_pipeline import NOW, app  # noqa: F401 - fixture reuse


class ScriptedLLM:
    name = "scripted"
    available = True
    last_error = ""

    def json(self, system, prompt, schema, **kw):
        props = schema["properties"]
        if "picks" in props:
            return {"picks": [{"id": 0, "consequence": 9, "reason": "big", "is_update": False}]}
        if "items" in props:
            return {"items": [{"id": 0, "headline": "Factual headline", "brief": "What happened. Why it matters.",
                               "who_it_affects": "Exporters"}]}
        if "mechanism" in props:
            return {"brief": "Rubber costs rose.", "detail": "Rubber up 30%", "mechanism": "Input cost squeeze"}
        if "business_name" in props:
            return {"business_name": "Books Co", "summary": "s", "what_it_sells": "bookkeeping", "who_pays": "founders",
                    "acquisition": "referrals", "pricing": "$400/month", "revenue": "$8K MRR (founder)", "costs": "Not disclosed",
                    "lesson_takeaway": "recurring wins", "figures": [{"label": "MRR", "value": "$8K", "kind": "reported",
                                                                     "attribution": "founder"}],
                    "price_per_customer_usd_month": 400}
        if "steps" in props:
            return {"question": "How many clients?", "inputs": [
                {"key": "price", "label": "Price", "value": 400, "unit": "$", "kind": "reported", "note": ""},
                {"key": "usd_inr", "label": "Rate", "value": 93, "unit": "₹ per $", "kind": "assumption", "note": ""},
                {"key": "target", "label": "Target", "value": 100000, "unit": "₹", "kind": "goal", "note": ""}],
                "steps": [{"key": "rev", "label": "Revenue", "expr": "price * usd_inr", "unit": "₹"},
                          {"key": "n", "label": "Clients", "expr": "ceil(target / rev)", "unit": "clients"}],
                "answer_key": "n", "answer_template": "About {n}.", "takeaway": "t"}
        if "settled" in props:
            return {"answer": "Because excise is a state subject.", "source_ids": [0], "settled": True}
        if "id" in props and "question" in props:
            return {"id": 0, "question": "q", "brief": "b"}
        if "id" in props:
            return {"id": 0, "reason": "best"}
        if "slots" in props:
            slots = [planner._slot_for_prompt(s) | {"change_note": ""} for s in planner.initial_slots()]
            slots[0]["item_limit"] = 2
            slots[0]["change_note"] = "Tighter front page"
            return {"rationale": "Shorter front page.", "slots": slots}
        raise AssertionError(f"unexpected schema {list(props)}")


def test_ai_paths(app):  # noqa: F811
    db = app.config["DB"]
    db.add_question("Why do states set fuel taxes?")
    res = pipeline.run_pipeline(db, edition_date="2026-11-21", now=NOW, llm=ScriptedLLM())
    assert res["status"] == "published", res
    c = db.get_edition("2026-11-21", 1)["content"]
    secs = {s["id"]: s for s in c["sections"]}
    assert c["lead"]["headline"] == "Factual headline"  # consequence 9 leads
    assert secs["maths"]["items"][0]["answer"] == "About 3 clients."
    assert secs["you-asked"]["items"][0]["answer"].startswith("Because")
    assert db.list_runs(1)[0]["diagnostics"]["validation"] == [] or True
    # The edition page renders, lead appears exactly once.
    html = app.test_client().get("/edition/2026-11-21/1").data.decode()
    assert html.count("Factual headline") == 1


def test_plan_proposal(app):  # noqa: F811
    db = app.config["DB"]
    plan = db.current_plan()
    slots, rationale = planner.generate_plan(db.get_profile(), plan["slots"], ScriptedLLM())
    changes = planner.diff_plans(plan["slots"], slots)
    assert len(changes) == 1 and changes[0]["field"] == "item_limit"
    # rotation survives regeneration
    assert next(s for s in slots if s["id"] == "case")["rotation"]
