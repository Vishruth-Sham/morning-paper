# The Morning Paper: notes for the agent

A local Flask + SQLite app. The human-facing README is deliberately short; operational details live here.

## Run

```bash
uv run morning-paper                 # serves http://127.0.0.1:5055/ (--port to change)
uv run morning-paper --generate      # make today's edition and exit
```

Without uv: `python3 -m venv .venv && .venv/bin/pip install -e . && .venv/bin/morning-paper`

To keep it running in the background and restart after code changes:

```bash
lsof -ti tcp:5055 | xargs kill; nohup uv run morning-paper > data-server.log 2>&1 &
```

Templates reload on their own; Python changes need a restart.

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
  maths.py        exercises as inputs + formulas, recomputed safely
  db.py           SQLite schema and queries
  seed.py         small generic defaults; overridden by paper/seed_local.py if present
  templates/      Jinja templates (Broadsheet design); static/styles.css
tests/            offline tests with stubbed fetchers
```

## Behaviour to preserve

- One issue per date; generating again does nothing unless a fresh issue is asked for. A failed run never
  touches a published issue. Each issue has a permanent URL `/edition/YYYY-MM-DD/<n>`.
- Links to originals open in a new tab (`target="_blank" rel="noopener"`).
- Saving the three answers redrafts the plan and applies it, keeping fields the reader edited by hand.

## Tests

```bash
uv run --extra dev pytest -q
```
