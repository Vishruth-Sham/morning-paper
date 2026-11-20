"""The Morning Paper — localhost web app."""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timedelta, timezone

from flask import Flask, abort, flash, redirect, render_template, request, url_for

from . import planner
from .db import DB
from .llm import LLM, LLMUnavailable
from .maths import fmt, indian
from .pipeline import run_pipeline, today_in
from .seed import EDITABLE_FIELDS, SEED_PROFILE

log = logging.getLogger(__name__)

PROFILE_CORE = ["interests", "goals", "always_cover", "reading_preference", "front_page_minutes", "business_minutes",
                "preferred_sources"]


class Jobs:
    """One background job at a time per kind (edition generation, plan drafting).
    sync=True runs jobs inline (used by tests)."""

    def __init__(self, sync: bool = False):
        self.lock = threading.Lock()
        self.jobs: dict[str, dict] = {}
        self.sync = sync

    def start(self, kind: str, fn, *args, **kw) -> bool:
        with self.lock:
            j = self.jobs.get(kind)
            if j and j["status"] == "running":
                return False
            self.jobs[kind] = {"status": "running", "started": datetime.now(timezone.utc), "result": None}

        def wrap():
            try:
                res = fn(*args, **kw)
                self.jobs[kind].update(status="done", result=res)
            except Exception as e:  # noqa: BLE001
                log.exception("job %s failed", kind)
                self.jobs[kind].update(status="error", result={"message": str(e)})
        if self.sync:
            wrap()
        else:
            threading.Thread(target=wrap, daemon=True).start()
        return True

    def get(self, kind: str) -> dict | None:
        return self.jobs.get(kind)


def create_app(db_path: str | None = None) -> Flask:
    app = Flask(__name__)
    app.secret_key = os.environ.get("MORNING_PAPER_SECRET", "local-only-morning-paper")
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    db = DB(db_path) if db_path else DB()
    ensure_seeded(db)
    jobs = Jobs(sync=os.environ.get("MORNING_PAPER_SYNC_JOBS") == "1")
    app.config["DB"] = db
    app.config["JOBS"] = jobs

    app.jinja_env.filters["inr"] = lambda v: "₹" + indian(v)
    app.jinja_env.filters["fmt"] = fmt
    app.jinja_env.filters["nicedate"] = nicedate
    app.jinja_env.filters["longdate"] = longdate
    app.jinja_env.filters["tojson_pretty"] = lambda v: json.dumps(v, indent=2, ensure_ascii=False)
    app.jinja_env.filters["sources_text"] = planner.sources_to_text

    def tz():
        return (db.get_profile() or SEED_PROFILE)["timezone"]

    @app.context_processor
    def inject():
        return {"today": today_in(tz()), "pending_questions": len(db.list_questions("pending"))}

    # ------------------------------------------------------------------ reading

    @app.get("/")
    def today():
        date = today_in(tz())
        ed = db.latest_edition_for(date)
        if ed:
            return render_edition(ed)
        job = jobs.get("edition")
        latest = db.list_editions()[:1]
        return render_template("today_empty.html", date=date, job=job, latest=latest[0] if latest else None)

    @app.get("/edition/<date>")
    def edition_by_date(date):
        ed = db.latest_edition_for(date)
        if not ed:
            abort(404)
        return render_edition(ed)

    @app.get("/edition/<date>/<int:issue_no>")
    def edition(date, issue_no):
        ed = db.get_edition(date, issue_no)
        if not ed:
            abort(404)
        return render_edition(ed)

    def render_edition(ed):
        c = ed["content"]
        lead = c.get("lead") or {}
        others = db.list_editions()
        issues_today = [e for e in others if e["date"] == ed["date"]]
        saved_urls = {n["source_url"] for n in db.list_notes()}
        ordered = sorted(others, key=lambda e: (e["date"], e["issue_no"]))
        volume_no = next((i + 1 for i, e in enumerate(ordered) if e["id"] == ed["id"]), len(ordered))
        next_date = (datetime.fromisoformat(ed["date"]) + timedelta(days=1)).date().isoformat()
        return render_template("edition.html", ed=ed, c=c, lead=lead, issues_today=issues_today,
                               saved_urls=saved_urls, volume_no=volume_no, next_date=next_date, active="today")

    @app.get("/archive")
    def archive():
        eds = db.list_editions()
        names = {}
        for e in eds:
            d = datetime.fromisoformat(e["date"])
            e["month"] = e["date"][:7]
            e["weekday"] = d.strftime("%a")
            names[e["month"]] = d.strftime("%B %Y")
        return render_template("archive.html", editions=eds, month_names=names, active="archive")

    # ------------------------------------------------------------------ generation

    @app.post("/generate")
    def generate():
        new_issue = request.form.get("new_issue") == "1"
        date = today_in(tz())
        if db.latest_edition_for(date) and not new_issue:
            flash("Today's edition is already published.")
            return redirect(url_for("today"))
        started = jobs.start("edition", run_pipeline, db, date, new_issue)
        if not started:
            flash("An edition is already being prepared.")
        if jobs.sync:
            return edition_outcome(jobs.get("edition"))
        return redirect(url_for("generating"))

    @app.get("/generating")
    def generating():
        job = jobs.get("edition")
        if not job:
            return redirect(url_for("today"))
        if job["status"] == "running":
            run = (db.list_runs(1) or [None])[0]
            return render_template("generating.html", job=job, run=run)
        return edition_outcome(job)

    def edition_outcome(job):
        res = (job or {}).get("result") or {}
        if res.get("status") == "published":
            return redirect(url_for("edition", date=res["date"], issue_no=res["issue_no"]))
        flash(f"Edition not published: {res.get('message', 'unknown error')}")
        return redirect(url_for("edit_advanced") + "#diagnostics")

    # ------------------------------------------------------------------ questions for tomorrow

    @app.post("/ask")
    def ask():
        text = (request.form.get("question") or "").strip()
        if text:
            db.add_question(text[:2000])
            flash("Saved. Tomorrow's paper will pick it up.")
        return redirect(request.form.get("next") or url_for("notebook") + "#questions")

    @app.post("/questions/<int:qid>/drop")
    def drop_question(qid):
        db.set_question_status(qid, "dropped")
        return redirect(url_for("notebook") + "#questions")

    # ------------------------------------------------------------------ notebook

    @app.get("/notebook")
    def notebook():
        return render_template("notebook.html", notes=db.list_notes(), questions=db.list_questions(), active="notebook")

    @app.route("/notebook/new", methods=["GET", "POST"])
    def note_new():
        if request.method == "POST":
            n = note_from_form(request.form)
            if not n["name"]:
                flash("Give the business a name.")
                return render_template("note_form.html", note=n)
            db.add_note(n)
            flash(f"Saved {n['name']} to your notebook.")
            return redirect(url_for("notebook"))
        # Prefill from an edition's case study.
        n = {f: "" for f in DB.NOTE_FIELDS}
        eid = request.args.get("edition_id", type=int)
        if eid:
            ed = db.get_edition_by_id(eid)
            if ed:
                case = next((it for s in ed["content"]["sections"] if s["kind"] == "case_study" for it in s["items"]), None)
                ex = next((it for s in ed["content"]["sections"] if s["kind"] == "exercise" for it in s["items"]), None)
                if case:
                    n.update(name=case.get("business_name") or case.get("headline", ""), what_it_sells=case.get("what_it_sells", ""),
                             revenue_model=f"{case.get('pricing', '')} · Who pays: {case.get('who_pays', '')}".strip(" ·"),
                             estimated_economics="; ".join(f"{f['label']}: {f['value']} ({f['kind']})" for f in case.get("figures", []))
                             + (f"\nDo the Maths: {ex['answer']}" if ex else ""),
                             acquisition_strategy=case.get("acquisition", ""), source_url=case.get("url", ""),
                             edition_id=eid, origin="edition")
        return render_template("note_form.html", note=n)

    @app.route("/notebook/<int:nid>/edit", methods=["GET", "POST"])
    def note_edit(nid):
        note = db.get_note(nid)
        if not note:
            abort(404)
        if request.method == "POST":
            db.update_note(nid, note_from_form(request.form))
            flash("Notebook entry updated.")
            return redirect(url_for("notebook"))
        return render_template("note_form.html", note=note)

    @app.post("/notebook/<int:nid>/delete")
    def note_delete(nid):
        db.delete_note(nid)
        flash("Entry removed.")
        return redirect(url_for("notebook"))

    # ------------------------------------------------------------------ edit my paper

    @app.get("/edit")
    def edit_paper():
        plan = db.current_plan()
        llm = LLM((db.get_profile() or {}).get("llm_provider", "auto"))
        return render_template("edit_simple.html", profile=db.get_profile(), plan=plan, job=jobs.get("plan"),
                               edition_job=jobs.get("edition"), llm=llm, active="edit",
                               today_published=bool(db.latest_edition_for(today_in(tz()))))

    @app.get("/edit/advanced")
    def edit_advanced():
        plan = db.current_plan()
        proposal = db.pending_proposal()
        changes = planner.diff_plans(plan["slots"], proposal["slots"]) if proposal else []
        llm = LLM((db.get_profile() or {}).get("llm_provider", "auto"))
        return render_template("edit.html", profile=db.get_profile(), plan=plan, plans=db.list_plans(),
                               proposal=proposal, changes=changes, runs=db.list_runs(8), job=jobs.get("plan"),
                               edition_job=jobs.get("edition"), llm=llm, fields=EDITABLE_FIELDS, active="edit",
                               today_published=bool(db.latest_edition_for(today_in(tz()))))

    @app.post("/edit/profile")
    def save_profile():
        old = db.get_profile()
        f = request.form
        new = {
            "interests": f.get("interests", "").strip(), "goals": f.get("goals", "").strip(),
            "always_cover": f.get("always_cover", "").strip(),
            "reading_preference": f.get("reading_preference", old["reading_preference"]).strip(),
            "front_page_minutes": int(f.get("front_page_minutes") or old["front_page_minutes"]),
            "business_minutes": int(f.get("business_minutes") or old["business_minutes"]),
            "timezone": f.get("timezone", old["timezone"]).strip() or "UTC",
            "preferred_sources": f.get("preferred_sources", old["preferred_sources"]).strip(),
            "llm_provider": f.get("llm_provider", old.get("llm_provider", "auto")),
        }
        db.save_profile(new)
        changed = [k for k in PROFILE_CORE if str(old.get(k)) != str(new.get(k))]
        simple = f.get("simple") == "1"
        if changed:
            if simple:
                jobs.start("plan", replan_and_apply, db, f"Your answers changed ({', '.join(changed)}).")
                flash("Saved. Tomorrow's paper follows your new answers.")
            elif jobs.start("plan", draft_proposal, db, f"Your answers changed ({', '.join(changed)})."):
                flash("Saved. Drafting proposed plan revisions — review them below before they apply.")
        else:
            flash("Saved.")
        return redirect(url_for("edit_paper") if simple else url_for("edit_advanced") + "#plan")

    @app.post("/edit/plan/regenerate")
    def regenerate_plan():
        if jobs.start("plan", draft_proposal, db, "You asked for a fresh AI draft of the plan."):
            flash("Drafting a fresh plan. It will appear as proposed changes.")
        return redirect(url_for("edit_advanced") + "#plan")

    @app.post("/edit/plan/slot/<slot_id>")
    def edit_slot(slot_id):
        plan = db.current_plan()
        f = request.form
        try:
            updates = {
                "section": f["section"].strip(), "slot": f["slot"].strip(), "purpose": f["purpose"].strip(),
                "sources": planner.parse_sources(f["sources"]), "selection_rule": f["selection_rule"].strip(),
                "frequency": f["frequency"], "freshness_hours": int(f["freshness_hours"] or 0),
                "display_format": f["display_format"], "item_limit": int(f["item_limit"] or 1),
                "length_limit_words": int(f["length_limit_words"] or 60), "enabled": f.get("enabled") == "on",
            }
        except (KeyError, ValueError) as e:
            flash(f"Not saved: {e}")
            return redirect(url_for("edit_advanced") + f"#slot-{slot_id}")
        slots = planner.edit_slot(plan["slots"], slot_id, updates)
        if slots == plan["slots"]:
            flash("No changes.")
        else:
            v = db.save_plan(slots, f"Edited {updates['slot']}", "manual", db.get_profile())
            flash(f"Saved as plan version {v}. The next edition will use it.")
        return redirect(url_for("edit_advanced") + f"#slot-{slot_id}")

    @app.post("/edit/plan/slot/<slot_id>/move")
    def move_slot(slot_id):
        plan = db.current_plan()
        slots = planner.move_slot(plan["slots"], slot_id, int(request.form.get("delta", 0)))
        if slots != plan["slots"]:
            db.save_plan(slots, "Reordered slots", "manual", db.get_profile())
        return redirect(url_for("edit_advanced") + "#plan")

    @app.post("/edit/proposal/<int:pid>")
    def apply_proposal(pid):
        prop = db.pending_proposal()
        if not prop or prop["id"] != pid:
            flash("That proposal is no longer pending.")
            return redirect(url_for("edit_advanced") + "#plan")
        if request.form.get("action") == "dismiss":
            db.set_proposal_status(pid, "dismissed")
            flash("Proposal dismissed. Your plan is unchanged.")
            return redirect(url_for("edit_advanced") + "#plan")
        accepted = [int(x) for x in request.form.getlist("accept")]
        plan = db.current_plan()
        if not accepted:
            flash("Nothing ticked, so nothing changed.")
            return redirect(url_for("edit_advanced") + "#plan")
        slots = planner.apply_changes(plan["slots"], prop["slots"], accepted)
        v = db.save_plan(slots, f"Applied {len(accepted)} proposed change(s)", "proposal", db.get_profile())
        db.set_proposal_status(pid, "applied")
        flash(f"Applied {len(accepted)} change(s) as plan version {v}.")
        return redirect(url_for("edit_advanced") + "#plan")

    @app.get("/edit/runs/<int:run_id>")
    def run_detail(run_id):
        run = next((r for r in db.list_runs(200) if r["id"] == run_id), None)
        if not run:
            abort(404)
        return render_template("run.html", run=run)

    @app.get("/edit/plan/<int:version>")
    def plan_version(version):
        p = db.get_plan(version)
        if not p:
            abort(404)
        return render_template("plan_version.html", p=p)

    return app


def draft_proposal(db: DB, reason: str) -> dict:
    profile = db.get_profile()
    plan = db.current_plan()
    llm = LLM(profile.get("llm_provider", "auto"))
    try:
        slots, rationale = planner.generate_plan(profile, plan["slots"], llm)
    except LLMUnavailable as e:
        return {"status": "error", "message": f"{e}. You can still edit the plan by hand."}
    changes = planner.diff_plans(plan["slots"], slots)
    if not changes:
        return {"status": "done", "message": "The AI found nothing to change."}
    pid = db.add_proposal(plan["id"], slots, f"{reason} {rationale}".strip())
    return {"status": "done", "proposal_id": pid, "changes": len(changes)}


def replan_and_apply(db: DB, reason: str) -> dict:
    """Simple mode: redraft the plan from the reader's answers and apply it straight away.
    Fields the reader edited by hand in the advanced table are kept."""
    profile = db.get_profile()
    plan = db.current_plan()
    llm = LLM(profile.get("llm_provider", "auto"))
    try:
        slots, rationale = planner.generate_plan(profile, plan["slots"], llm)
    except LLMUnavailable as e:
        return {"status": "error", "message": f"{e} Your answers are still used when the paper is written."}
    changes = planner.diff_plans(plan["slots"], slots)
    accept = [i for i, c in enumerate(changes) if not c["manually_edited"]]
    if not accept:
        return {"status": "done", "message": "Nothing in the plan needed to change."}
    new = planner.apply_changes(plan["slots"], slots, accept)
    v = db.save_plan(new, f"Updated from your answers. {rationale}".strip()[:300], "ai", profile)
    return {"status": "done", "plan_version": v, "changes": len(accept)}


def note_from_form(f) -> dict:
    return {k: (f.get(k) or "").strip() for k in ["name", "what_it_sells", "revenue_model", "estimated_economics",
                                                  "acquisition_strategy", "copy_this", "notes", "source_url"]} | {
        "edition_id": f.get("edition_id", type=int) if hasattr(f, "get") else None,
        "origin": f.get("origin") or "manual"}


def nicedate(iso: str | None, with_year: bool = True) -> str:
    if not iso:
        return ""
    try:
        d = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    return d.strftime("%-d %b %Y" if with_year else "%-d %b")


def longdate(iso: str | None, weekday: bool = False) -> str:
    if not iso:
        return ""
    try:
        d = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    return d.strftime(("%A " if weekday else "") + "%-d %B %Y")


def ensure_seeded(db: DB) -> None:
    if not db.get_profile():
        db.save_profile(SEED_PROFILE)
    if not db.current_plan():
        db.save_plan(planner.initial_slots(), "Seeded from your brief", "seed", db.get_profile())
    if not db.list_editions() and not os.environ.get("MORNING_PAPER_NO_SAMPLE"):
        try:  # a hand-made first edition, kept locally only
            from . import seed_edition
        except ImportError:
            return
        seed_edition.publish(db)


def main():
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="The Morning Paper")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 5055)))
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--generate", action="store_true", help="Generate today's edition and exit")
    args = ap.parse_args()
    app = create_app()
    if args.generate:
        print(json.dumps(run_pipeline(app.config["DB"]), indent=2, default=str))
        return
    print(f"The Morning Paper is at http://{args.host}:{args.port}/")
    app.run(host=args.host, port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
