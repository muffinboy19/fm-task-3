# Open Source Issue Solver for Go Repositories

Agent that reads a GitHub issue from an open-source Go repo, plans and generates a fix, validates the patch against the plan, and writes a PR summary — for gin, cobra, validator, golangci-lint, and similar projects.

## Architecture

**Step 1 — Issue understanding**  
Fetches the issue and comments from GitHub and extracts identifiers, paths, and error strings.  
Optional LLM intake turns that into structured bug type, repro steps, and confidence.

**Step 2 — Context builder**  
Clones or uses the repo under `test_repo/<repo>/` and greps for relevant symbols and files.  
Slices only the candidate functions and repo conventions into a small context bundle.

**Step 3 — Fix plan**  
Cursor (or Gemini) reads the issue + context and writes a surgical fix plan in `output/plan.md`.  
Keeps scope tight: which files to change and what behavior to fix.

**Step 4 — Patch generation**  
The LLM produces a unified diff (`output/fix.patch`) aligned with the plan and Go style.

**Step 5 — Plan adherence**  
An LLM compares `plan.md` and `fix.patch` for alignment (files, approach, tests).  
Writes `output/plan_check.json`; warns if the patch diverges from the plan.

**Step 6 — Validation (Go testing)**  
Applies the patch, runs `go build` on affected packages, then scoped `go test` (or `-run` when the plan names tests).  
Writes `output/validation_report.json`. Use `--validation-full` for `go test ./...`.

**Step 7 — PR summary**  
The LLM drafts title and body for a pull request in `output/pr_summary.md`.  
References the issue, plan, patch, and validation outcome.

```
main.py → issue_understanding → context_builder → code_reasoning_agent
       → code_generator → plan_adherence_checker → validator → pr_writer
```

## Live UI

```bash
python main.py
```

Opens **http://127.0.0.1:8765/** in your browser. Paste a GitHub issue URL and click **Run**. The server stays up — use **Retry** or **New issue** for the next run.

To run a single issue from the terminal:

```bash
python main.py --issue https://github.com/owner/repo/issues/123
```

Disable auto-opening the browser with `--no-ui`. Disable the dashboard during CLI runs with `DASHBOARD_UI=false` in `.env`.

## Setup

**Step 1 — Install**

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

**Step 2 — Create `.env`**

```bash
cp .env.example .env
```

**Step 3 — Configure `.env`**

Add your Cursor LLM settings:

```
LLM_PROVIDER=cursor
CURSOR_API_KEY=your_cursor_api_key_here
CURSOR_MODEL=composer-2.5
```

Optional: `GITHUB_TOKEN` in `.env` for fetching private issues during the pipeline.

**Step 4 — Run**

```bash
python main.py
```

The UI opens in your browser — paste an issue URL and click Run. Results land in `output/` and `logs/run_report.md`.

Or run one issue from the terminal:

```bash
python main.py --issue https://github.com/gin-gonic/gin/issues/1234
```
