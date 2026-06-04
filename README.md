# go-issue-solver

Agentic AI contributor for open-source Go projects. Takes a GitHub issue URL and a local clone of an approved repo, then:

1. **Understands the issue** (GitHub API + heuristics, no LLM)
2. **Builds surgical context** (grep, optional call-graph, function slicing, conventions)
3. **Plans the fix** (LLM)
4. **Generates a unified diff** (LLM)
5. **Validates** (`git apply` + `go test ./...`, with retries)
6. **Writes a PR summary** (LLM)

Approved target projects: [gin-gonic/gin](https://github.com/gin-gonic/gin), [spf13/cobra](https://github.com/spf13/cobra), [go-playground/validator](https://github.com/go-playground/validator), [golangci/golangci-lint](https://github.com/golangci/golangci-lint).

## Architecture

```
main.py
  ├── modules/issue_understanding.py   # GitHub fetch, identifier extraction
  ├── modules/context_builder.py     # grep + optional code-review-graph + Go slice
  ├── modules/code_reasoning_agent.py  # LLM fix plan
  ├── modules/code_generator.py        # LLM unified diff
  ├── modules/validator.py             # apply patch, go test, retry loop
  └── modules/pr_writer.py             # LLM PR title/body
```

Prompts live in `prompts/` and can be edited without changing Python code.

## Prerequisites

- Python 3.10+
- [Go](https://go.dev/dl/) (for validation)
- [Git](https://git-scm.com/) — target repo must be a **git clone** (validator resets with `git checkout`)
- Google Gemini API key (`GEMINI_API_KEY` in `.env`)
- Optional: `GITHUB_TOKEN` (higher GitHub API rate limits)
- Optional: [`code-review-graph`](https://github.com/) CLI for blast-radius expansion (falls back to grep-only)

## Setup

```bash
cd pocketfmTask
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# Clone an approved project (example: gin)
git clone https://github.com/gin-gonic/gin.git ./gin

# Configure credentials and targets
cp .env.example .env
# Edit .env — set GEMINI_API_KEY, GITHUB_ISSUE_URL, GITHUB_REPO_PATH
```

## Run

Fill in `.env`, then:

```bash
python main.py
```

CLI flags override `.env` values:

```bash
python main.py \
  --issue https://github.com/gin-gonic/gin/issues/XXXX \
  --repo ./gin
```

### `.env` variables

| Variable | Required | Description |
|----------|----------|-------------|
| `GEMINI_API_KEY` | Yes | Google Gemini API key |
| `GEMINI_MODEL` | No | Default: `gemini-2.0-flash` |
| `GITHUB_ISSUE_URL` | Yes | Full GitHub issue URL |
| `GITHUB_REPO_PATH` | Yes | Local path to cloned repo |
| `GITHUB_TOKEN` | No | GitHub API token |
| `OUTPUT_DIR` | No | Default: `./output` |
| `LOG_DIR` | No | Default: `./logs` |
| `DRY_RUN` | No | `true` to skip git apply / go test |

### Flags

| Flag | Description |
|------|-------------|
| `--issue` | GitHub issue URL (or `GITHUB_ISSUE_URL`) |
| `--repo` | Path to local git clone (or `GITHUB_REPO_PATH`) |
| `--output` | Output directory (or `OUTPUT_DIR`) |
| `--log-dir` | Log directory (or `LOG_DIR`) |
| `--api-key` | Gemini key (or `GEMINI_API_KEY`) |
| `--github-token` | GitHub token (or `GITHUB_TOKEN`) |
| `--dry-run` | Skip patch apply and `go test` |

## Outputs

After a run, `output/` contains:

| File | Description |
|------|-------------|
| `fix.patch` | Unified diff (best attempt after validation) |
| `plan.md` | LLM fix plan |
| `pr_summary.md` | Suggested PR title and body |
| `run_summary.json` | Issue URL, files in scope, validation status |

## Logs

Every run writes detailed logs to `logs/`:

| File | Description |
|------|-------------|
| **`logs/run_report.md`** | **Main log doc** — step-by-step markdown report (open this first) |
| `logs/agent_YYYYMMDD_HHMMSS.log` | Full timestamped log with every action |
| `logs/latest.log` | Copy of the most recent run log |

## Design notes

- **Surgical context** keeps LLM input small: full bodies only for top candidate functions, signatures for neighbors.
- **Zero-LLM stages** (issue parsing, grep, slicing) run before any model calls.
- **Validation loop** re-prompts the generator with `git apply` / `go test` errors (up to 2 retries).
- Opening a real GitHub PR is optional; a local patch + PR summary satisfies the assignment.

## Example (dry-run, no Go tests)

```bash
python main.py \
  --issue https://github.com/spf13/cobra/issues/1234 \
  --repo ./cobra \
  --dry-run
```

## Project layout

```
.
├── main.py                 # CLI orchestrator
├── modules/                # Agent pipeline
├── prompts/                # Editable LLM prompts
├── requirements.txt
├── output/                 # Generated artifacts (gitignored)
└── README.md
```

## License

Submission for take-home evaluation.
