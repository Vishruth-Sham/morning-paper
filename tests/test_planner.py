from paper import planner


def test_manual_edit_is_tracked_and_survives_proposals():
    slots = planner.initial_slots()
    edited = planner.edit_slot(slots, "news", {"item_limit": 2})
    s = next(s for s in edited if s["id"] == "news")
    assert s["item_limit"] == 2 and "item_limit" in s["edited_fields"]

    proposed = planner.initial_slots()
    next(p for p in proposed if p["id"] == "news")["item_limit"] = 4
    next(p for p in proposed if p["id"] == "explainer")["purpose"] = "New purpose"
    changes = planner.diff_plans(edited, proposed)
    by_field = {(c["slot_id"], c["field"]): i for i, c in enumerate(changes)}
    assert changes[by_field[("news", "item_limit")]]["manually_edited"]
    # Reader accepts only the discover change: their manual edit stays.
    applied = planner.apply_changes(edited, proposed, [by_field[("explainer", "purpose")]])
    assert next(s for s in applied if s["id"] == "news")["item_limit"] == 2
    assert next(s for s in applied if s["id"] == "explainer")["purpose"] == "New purpose"


def test_sources_round_trip():
    slots = planner.initial_slots()
    for s in slots:
        text = planner.sources_to_text(s["sources"])
        parsed = planner.parse_sources(text)
        assert [p["url"] for p in parsed] == [x["url"] for x in s["sources"]]
        assert [p.get("link_pattern", "") for p in parsed] == [x.get("link_pattern", "") for x in s["sources"]]


def test_move_slot():
    slots = planner.initial_slots()
    moved = planner.move_slot(slots, "explainer", -1)
    assert moved[0]["id"] == "explainer"
