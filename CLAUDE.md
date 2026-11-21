# The Morning Paper: notes for the agent

A local Flask + SQLite app. The human-facing README is deliberately short; operational details live here.

## Run

```bash
uv run morning-paper                 # serves http://127.0.0.1:5055/ (--port to change)
uv run morning-paper --generate      # make today's edition and exit
uv run morning-paper --apply-local   # push paper/seed_local.py changes into the existing database, then restart
```

Without uv: `python3 -m venv .venv && .venv/bin/pip install -e . && .venv/bin/morning-paper`

To keep it running in the background and restart after code changes:

```bash
lsof -ti tcp:5055 | xargs kill; nohup uv run morning-paper > data-server.log 2>&1 &
```

Templates reload on their own; Python changes need a restart.

Mac app: `mac/build-app.sh --dock` builds `~/Applications/Morning Paper.app` (a small Swift/WebKit window,
`mac/MorningPaper.swift`, compiled with `swiftc`) and pins it to the Dock. On launch it starts the server if
needed, POSTs `/generate` (only makes a paper if today has none) and shows the paper; clicking the Dock icon again
brings the window back, and links to originals open in the default browser. The project and uv paths are written
into Info.plist, so re-run the build if either moves. The reader doesn't use Terminal; run it for them.

## The writer (Claude)

Story selection, briefs, the maths exercise, notebook answers and plan redrafts use Claude when one is
available, checked in this order: `ANTHROPIC_API_KEY`, then a signed-in `claude` CLI. Without either the
paper still runs on fixed rules. Edit my paper → Advanced shows which writer is active.

| Variable | Default | Purpose |
| --- | --- | --- |
| `PORT` | `5055` | Port (or `--port`) |
| `MORNING_PAPER_DB` | `data/paper.db` | Database location |
| `MORNING_PAPER_MODEL` | `claude-opus-5-5` | Model for the writer |
| `ANTHROPIC_API_KEY` | none | Writer via the API |
| `MORNING_PAPER_WORKERS` | `4` | Sections made at the same time |

## Privacy rules (important)

- Never commit anything personal: `data/`, `*.db`, logs, `.env`, `paper/seed_local.py` (the reader's own
  starting profile, rotation and plan) and `paper/seed_edition.py` (a hand-made first edition). All are in
  `.gitignore`. Check `git ls-files` before pushing.
- Don't write the reader's interests, goals, sources or targets into code, prompts, tests or docs. Prompts
  read them from the profile at run time. Tests use the neutral plan in `tests/plan_fixture.py`.
- The README screenshot uses made-up stories. Keep it that way.

## Layout

```
paper/
  app.py          routes and pages (Today, Archive, Notebook, Edit my paper, Advanced)
  pipeline.py     fetch → filter & group → select → compose → validate → publish
  planner.py      profile → plan; per-field manual edits; proposals
  fetchers.py     RSS, Google News (with link decoding), page extraction, Wikipedia
  extras.py       non-article sections: tasks (local task app), weather (Open-Meteo), markets, daily prompts
  maths.py        exercises as inputs + formulas, recomputed safely
  db.py           SQLite schema and queries
  seed.py         small generic defaults; overridden by paper/seed_local.py if present
  templates/      Jinja templates (Broadsheet design); static/styles.css
tests/            offline tests with stubbed fetchers
```

## Sections and layout

Each plan slot has a `kind` (news, explainer, case_study, exercise, questions, long_read, tasks, weather,
markets, prompt) and an optional `placement`: `top` puts it in the compact "Your day" strip above the front
page, `more` puts it in the "More for you" grid of headlines below Business, and anything else is the
normal front page/Business layout. Only main news can lead. Slots run in parallel
(`MORNING_PAPER_WORKERS`, default 4). Settings for the extra kinds (places, symbols, env file, ideas) live
on the slot itself and survive plan redrafts.

## Behaviour to preserve

- One issue per date; generating again does nothing unless a fresh issue is asked for. A failed run never
  touches a published issue. Each issue has a permanent URL `/edition/YYYY-MM-DD/<n>`.
- Every scheduled section must show up. A section that comes back empty is retried once with a wider search
  (Startup Story also tries all case studies on the topic, then the next business types in the rotation; Do the
  Maths falls back to the Concept of the day company). `check_rules` then checks the issue (links, Startup Story
  fields, reported vs estimate, worked maths, concept + real case) and anything still missing is listed on the
  page as "Missing today" and in the run diagnostics. Never drop a section silently.
- Links to originals open in a new tab (`target="_blank" rel="noopener"`).
- Saving the three answers redrafts the plan and applies it, keeping fields the reader edited by hand.

## Tests

```bash
uv run --extra dev pytest -q
```
