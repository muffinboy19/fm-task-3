# Live dashboard

Visual UI for the Open Source Issue Solver pipeline.

## With a pipeline run

```bash
python main.py
```

Start the UI (issue URL + live dashboard):

```bash
python main.py
```

Opens **http://127.0.0.1:8765/** (auto-refreshes every second). Use `--no-ui` to skip opening the browser.

## Dashboard only (watch an existing run)

```bash
python -m modules.dashboard_server
```

Requires `logs/live_state.json` (written automatically when the agent runs).

## What you see

- **Pipeline** — step 1–7 status (pending / running / ok / fail / warn)
- **Issue / Context / Plan / Diff / Validation / PR** — output artifacts
- **Diff** — colorized unified diff (+ green / − red)
- **Logs** — tail of the current agent log
- **Events & artifacts** — sidebar timeline
