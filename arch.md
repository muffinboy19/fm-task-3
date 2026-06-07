# How this tool works (simple version)

Imagine you hired a tiny robot team to fix a bug in a Go project on GitHub.

---

## The story in 7 steps

```
GitHub issue URL
       │
       ▼
  ① Understand the bug
       │
       ▼
  ② Find the right files
       │
       ▼
  ③ Write a fix plan
       │
       ▼
  ④ Write the code patch
       │
       ▼
  ⑤ Check plan vs patch
       │
       ▼
  ⑥ Run Go tests
       │
       ▼
  ⑦ Write PR description
```

---

### Step 1 — Read the issue (`IssueUnderstanding`)

You give a GitHub issue link, like `https://github.com/some/repo/issues/123`.

The robot reads the title, body, and comments. It figures out:
- What is broken?
- What should happen instead?
- Any file names or error messages mentioned?

It saves this as `issue_understanding.json`.

---

### Step 2 — Find code (`ContextBuilder`)

The robot clones the repo into `test_repo/` (created automatically, not committed).

Then it searches the codebase:
- Look at paths mentioned in the issue
- Grep for function names and error strings
- Pick the most relevant `.go` files and function bodies

It saves a short list: “these files, these functions, this is how the repo writes tests.”

---

### Step 3 — Make a plan (`CodeReasoningAgent`)

An LLM reads the issue + context and writes `plan.md`:
- Which files to change
- What the fix should do
- Which tests to add or update

Think of this as the robot’s homework before touching code.

---

### Step 4 — Write the patch (`CodeGenerator`)

Another LLM turn produces `fix.patch` — a git diff.

If the diff is broken, the robot retries (up to 4 times):
1. Try a normal unified diff
2. Fall back to “edit whole files, then `git diff`”

---

### Step 5 — Did we follow the plan? (`PlanAdherenceChecker`)

An LLM compares `plan.md` to `fix.patch`.

If the patch drifts (wrong files, missing tests), the robot tries to regenerate.

---

### Step 6 — Does it actually work? (`Validator`)

This is the real test. On a clean copy of the repo:

1. `git apply` the patch — does it apply cleanly?
2. `go build` — does it compile?
3. `go test` — do the relevant tests pass?

Then it **undoes** the patch so the repo is clean again.

Saves `validation_report.json`.

---

### Step 7 — PR text (`PRWriter`)

If validation passed, the robot writes `pr_summary.md` — a pull request description you can paste into GitHub.

If validation failed, it writes a **draft** warning you not to merge yet.

---

## The folders

| Folder / file | What it is |
|---------------|------------|
| `main.py` | Starts the 7-step pipeline |
| `modules/` | One module per step (understand, context, plan, patch, check, validate, PR) |
| `prompts/` | Text instructions sent to the LLM |
| `test_repo/` | Cloned Go repos (gitignored, recreated per run) |
| `output/` | Results of the latest run (patch, plan, reports) |
| `logs/` | Detailed run logs + live dashboard files |
| `ui/` | Small web page to watch progress live |
| `scripts/eval_*.py` | Batch tests on many GitHub issues |

---

## The LLM

The robot’s “brain” is an LLM (default: Cursor `composer-2.5`, or Gemini).

Config lives in `.env`:
- `GITHUB_ISSUE_URL` — optional; only used with `python main.py --issue …`
- `CURSOR_API_KEY` or `GEMINI_API_KEY`
- `TEST_REPO_DIR` — where clones go (default `test_repo`)

---

## How to run

```bash
source .venv/bin/activate
python main.py
```

Opens the local UI at `http://127.0.0.1:8765/` — paste an issue URL, click Run, watch the pipeline live.

Or run one issue from the terminal:

```bash
python main.py --issue https://github.com/owner/repo/issues/123
```

---

## One sentence summary

**Read GitHub issue → find Go code → plan fix → write patch → test it → write PR text.**

That’s the whole machine.
