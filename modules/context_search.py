"""
Curated search plan for ContextBuilder (fast file discovery).

Ladder: explicit paths → ripgrep on strong symbols → tests in same package.
"""

import re
from pathlib import Path

# Prose words often matched by PascalCase regex in issue bodies
_STOP_WORDS = frozenset({
    "The", "This", "When", "If", "It", "In", "For", "Is", "Go", "JSON", "HTTP",
    "API", "Error", "Panic", "Note", "TODO", "Fix", "See", "Use", "New", "Get",
    "Set", "Has", "Add", "Description", "Example", "Input", "After", "Expected",
    "Related", "Version", "Can", "Yes", "Steps", "Render", "Observe", "Source",
    "Code", "Minimal", "Operating", "System", "Linux", "Thank", "Extension",
    "Test", "Results", "Original", "Rendered", "Decoded", "Round", "Output",
    "Non", "Character", "Corruption", "Issue", "Confirmed", "Scenario", "Result",
    "Working", "Corrupted", "PR", "Thanks", "Feel", "Per", "UTF", "BMP", "ASCII",
    "Unicode", "FFFF", "RFC", "CJK", "Offending", "Write", "WriteByte", "Appendf",
    "Unmarshal", "MaxASCII", "BytesToString", "NewRecorder", "Data", "Body",
    "Println", "Printf",
    # Issue prose / labels (not code symbols)
    "That", "Personally", "You", "There", "Schema", "Feature", "Generate",
    "README", "Would", "Could", "Should", "From", "With", "What", "How",
})

_TITLE_SYMBOL_RE = re.compile(r"\b([A-Z][a-zA-Z0-9]*(?:\.[A-Z][a-zA-Z0-9]*)+)\b")


def anchor_paths(issue: dict) -> list[str]:
    u = issue.get("understanding") or {}
    anchors = u.get("anchors") or {}
    paths = list(anchors.get("paths") or [])
    raw_anchors = (issue.get("raw") or {}).get("anchors") or {}
    for p in raw_anchors.get("paths") or []:
        if p not in paths:
            paths.append(p)
    return [p.strip() for p in paths if p and p.strip().endswith(".go")]


def curated_grep_terms(issue: dict, max_terms: int = 10) -> list[str]:
    """Strong symbols only — not full noisy identifier lists."""
    u = issue.get("understanding") or {}
    anchors = u.get("anchors") or {}
    candidates: list[str] = []

    for term in (
        list(anchors.get("identifiers") or [])
        + list(anchors.get("backtick_terms") or [])
    ):
        if _is_grep_worthy(term):
            candidates.append(term.strip())

    for m in _TITLE_SYMBOL_RE.finditer(issue.get("title") or ""):
        candidates.append(m.group(1))

    for err in anchors.get("error_strings") or issue.get("error_strings") or []:
        if err and len(err) >= 4:
            candidates.append(err.strip())

    def rank(term: str) -> tuple[int, int]:
        score = 0
        if "AsciiJSON" in term or "ascii" in term.lower():
            score += 10
        if "." in term:
            score += 5
        if "/" in term:
            score -= 5
        return (-score, len(term))

    candidates.sort(key=rank)
    out: list[str] = []
    seen: set[str] = set()
    for t in candidates:
        key = t.lower()
        if key not in seen:
            seen.add(key)
            out.append(t)
        if len(out) >= max_terms:
            break
    return out[:max_terms]


def fallback_grep_terms(issue: dict, max_terms: int = 8) -> list[str]:
    """Domain terms from title/body when curated identifiers miss the repo."""
    title = issue.get("title") or ""
    body = issue.get("body") or ""
    blob = f"{title}\n{body}"
    candidates: list[str] = []

    if re.search(r"json\s*schema", blob, re.I):
        candidates.extend(["jsonschema", "JSONSchema", "Reflect"])
    if re.search(r"struct\s+tags?", blob, re.I):
        candidates.extend(["validate", "parseFieldTags", "extractStructCache"])
    if re.search(r"invopop", blob, re.I):
        candidates.append("invopop")

    for term in re.findall(r"`([^`]{3,60})`", blob):
        if _is_grep_worthy(term):
            candidates.append(term.strip())

    for m in _TITLE_SYMBOL_RE.finditer(title):
        sym = m.group(1)
        if _is_grep_worthy(sym):
            candidates.append(sym)

    out: list[str] = []
    seen: set[str] = set()
    for t in candidates:
        key = t.lower()
        if key not in seen:
            seen.add(key)
            out.append(t)
        if len(out) >= max_terms:
            break
    return out


def curated_error_strings(issue: dict) -> list[str]:
    u = issue.get("understanding") or {}
    anchors = u.get("anchors") or {}
    errs = list(anchors.get("error_strings") or []) + list(
        issue.get("error_strings") or []
    )
    return list(dict.fromkeys(e for e in errs if e and len(e) >= 4))


def resolve_paths(repo: Path, rel_paths: list[str]) -> list[Path]:
    """Map issue anchor paths to files under repo."""
    found: list[Path] = []
    seen: set[str] = set()
    for rel in rel_paths:
        p = (repo / rel).resolve()
        try:
            p.relative_to(repo.resolve())
        except ValueError:
            p = None
        if p and p.is_file() and p.suffix == ".go":
            key = str(p)
            if key not in seen:
                seen.add(key)
                found.append(p)
            continue
        name = Path(rel).name
        matches = [
            m for m in repo.rglob(name)
            if m.is_file() and "vendor" not in m.parts
        ]
        if len(matches) == 1:
            key = str(matches[0])
            if key not in seen:
                seen.add(key)
                found.append(matches[0])
    return found


def _is_grep_worthy(term: str) -> bool:
    if not term or len(term) < 3 or len(term) > 80:
        return False
    if "\n" in term or " " in term.strip():
        return False
    if term.startswith("#") or term.startswith("###"):
        return False
    if term.endswith(".go"):
        return False
    if term in _STOP_WORDS:
        return False
    if "." in term and not term.startswith("."):
        return True
    if re.match(r"^[a-z][\w./]*$", term):
        return True
    if re.match(r"^[A-Z][a-zA-Z0-9]{4,}$", term) and term not in _STOP_WORDS:
        return True
    if re.match(r"^[A-Z][a-zA-Z0-9]*\.[A-Z]", term):
        return True
    if "\\u" in term or "%04x" in term:
        return len(term) <= 40
    if re.match(r"^v?\d+\.\d+", term) or re.match(r"^go\d", term, re.I):
        return False
    if term.lower() in ("fmt", "json", "data", "render", "code", "test"):
        return False
    if term.endswith(".String") or "..." in term or "(" in term:
        return False
    return False
