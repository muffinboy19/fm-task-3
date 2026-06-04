## Project conventions (required)

You MUST match this repository's existing style. Do not invent new patterns.

Naming & layout
- Match existing function, type, test, and file names in the scoped package (e.g. TestRenderXxx for render tests).
- Keep receiver names, parameter names, and package layout consistent with neighboring code.
- New tests belong in the same *_test.go file and package as related tests.

Testing
- Use the same test framework already used in that package (e.g. testify require/assert if present).
- Follow existing setup: httptest, table tests, t.Run subtests — copy the same structure as sibling tests.
- Assert the same way the project does (exact body strings, json.Unmarshal round-trip, etc.).

Implementation
- Minimal diff only: change what the issue requires; do not refactor unrelated code.
- Reuse existing helpers, imports, and patterns (buffer reuse, error returns, formatting verbs).
- Match literal formats already in tests (escape style, content types, JSON shape).

If context shows example functions or tests, treat them as the source of truth for style.
