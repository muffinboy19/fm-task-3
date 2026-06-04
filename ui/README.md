# Live dashboard

Visual UI for the go-issue-solver pipeline.

## With a pipeline run

```bash
python main.py --ui --no-reset
```

Open **http://127.0.0.1:8765/** in your browser. The page auto-refreshes every second.

## Dashboard only (watch an existing run)

```bash
python -m modules.dashboard_server
```

Requires `logs/live_state.json` (written automatically when the agent runs).

## What you see

- **Pipeline** — step 1–6 status (pending / running / ok / fail)
- **Issue / Context / Plan / Diff / Validation / PR** — output artifacts
- **Diff** — colorized unified diff (+ green / − red)
- **Logs** — tail of the current agent log
- **Events & artifacts** — sidebar timeline
