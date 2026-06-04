"""
Module 2 — Context Builder

Fast file discovery (zero LLM):
  1. Explicit paths from issue understanding (instant)
  2. ripgrep on curated symbols only (small term list)
  3. Sibling *_test.go in same package as hits
  4. code-review-graph (optional) — blast-radius expansion
  5. slice function bodies + neighbor signatures
"""

import json
import subprocess
from pathlib import Path

from modules.agent_logger import get_logger
from modules.convention import format_conventions_block
from modules.context_search import (
    anchor_paths,
    curated_grep_terms,
    curated_error_strings,
    resolve_paths,
)

_MAX_IMPL_FILES = 5
_MAX_TOTAL_FILES = 8
_PATH_ANCHOR_SCORE = 100
_TEST_SIBLING_SCORE = 40
_ERROR_HIT_BONUS = 3


class ContextBuilder:
    def __init__(self, repo_path: Path):
        self.repo = repo_path.resolve()
        self._crg_available = self._check_crg()

    def build(self, issue: dict) -> dict:
        log = get_logger()
        log.info(f"Repo path: {self.repo}")
        log.kv("code-review-graph available", self._crg_available)

        rel_paths = anchor_paths(issue)
        grep_terms = issue.get("grep_terms") or curated_grep_terms(issue)
        error_strings = curated_error_strings(issue)

        log.kv("Anchor paths", rel_paths)
        log.kv("Curated grep terms", grep_terms)
        log.kv("Error strings", error_strings)

        hits = self._discover_candidate_files(rel_paths, grep_terms, error_strings)
        candidate_files = [p for p, _ in hits]

        log.info(f"Located {len(candidate_files)} candidate file(s)")
        for f in candidate_files:
            log.debug(f"  file: {f.relative_to(self.repo)}")

        if self._crg_available and candidate_files:
            log.info("Expanding candidates with code-review-graph...")
            candidate_files = self._expand_with_crg(candidate_files, issue)
            log.info(f"After CRG expansion: {len(candidate_files)} file(s)")

        score_terms = grep_terms + error_strings
        candidate_functions = self._extract_candidate_functions(
            candidate_files, score_terms
        )
        candidate_functions = self._attach_neighbor_signatures(candidate_functions)
        convention_snapshot = self._extract_conventions(candidate_functions)
        repo_tree = self._build_compact_tree()

        raw_context_str = self._assemble_context_string(
            candidate_functions, convention_snapshot, repo_tree, issue
        )

        log.info(f"Extracted {len(candidate_functions)} candidate function(s)")
        for fn in candidate_functions:
            log.debug(
                f"  {fn['name']} in {fn['file']} "
                f"(lines {fn['start_line']}-{fn['end_line']}, score={fn.get('score', 0)})"
            )
        log.block("Convention snapshot", convention_snapshot)
        log.block("Assembled LLM context", raw_context_str)

        return {
            "candidate_files": [
                str(p.relative_to(self.repo)) for p in candidate_files
            ],
            "grep_terms_used": grep_terms,
            "anchor_paths_used": rel_paths,
            "candidate_functions": candidate_functions,
            "convention_snapshot": convention_snapshot,
            "repo_tree": repo_tree,
            "raw_context_str": raw_context_str,
        }

    def _discover_candidate_files(
        self,
        rel_paths: list[str],
        grep_terms: list[str],
        error_strings: list[str],
    ) -> list[tuple[Path, int]]:
        """Return (path, score) ranked highest first."""
        log = get_logger()
        scores: dict[Path, int] = {}

        def add(path: Path, points: int) -> None:
            if not path.exists() or "vendor" in path.parts:
                return
            if path.suffix != ".go":
                return
            scores[path] = scores.get(path, 0) + points

        path_hits = resolve_paths(self.repo, rel_paths)
        anchor_dirs: set[Path] = set()
        for p in path_hits:
            add(p, _PATH_ANCHOR_SCORE)
            anchor_dirs.add(p.parent)
            log.debug(f"  path anchor: {p.relative_to(self.repo)}")

        impl_count = lambda: len([p for p in scores if "_test.go" not in p.name])
        if impl_count() < _MAX_IMPL_FILES:
            for path, pts in self._grep_hits(
                grep_terms,
                error_strings,
                search_dirs=anchor_dirs if anchor_dirs else None,
            ):
                add(path, pts)

        impl_files = [p for p in scores if "_test.go" not in p.name]
        for impl in sorted(impl_files, key=lambda p: scores[p], reverse=True)[
            :_MAX_IMPL_FILES
        ]:
            for test_file in self._sibling_test_files(impl, grep_terms):
                add(test_file, _TEST_SIBLING_SCORE)

        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        return self._cap_file_list(ranked)

    def _cap_file_list(self, ranked: list[tuple[Path, int]]) -> list[tuple[Path, int]]:
        """Prefer impl files; tests only from same package dir as top impl."""
        impls = [(p, s) for p, s in ranked if "_test.go" not in p.name]
        tests = [(p, s) for p, s in ranked if "_test.go" in p.name]
        chosen: list[tuple[Path, int]] = impls[:_MAX_IMPL_FILES]
        if impls:
            anchor_dir = impls[0][0].parent
            for p, s in tests:
                if p.parent == anchor_dir and len(chosen) < _MAX_TOTAL_FILES:
                    chosen.append((p, s))
        return chosen[:_MAX_TOTAL_FILES]

    def _grep_hits(
        self,
        grep_terms: list[str],
        error_strings: list[str],
        search_dirs: set[Path] | None = None,
    ) -> list[tuple[Path, int]]:
        hits: dict[Path, int] = {}
        log = get_logger()
        roots = [str(d) for d in search_dirs] if search_dirs else [str(self.repo)]

        for term in grep_terms:
            if len(term) < 3:
                continue
            log.debug(f"grep term: {term!r} in {len(roots)} dir(s)")
            for root in roots:
                try:
                    result = subprocess.run(
                        ["grep", "-rl", "--include=*.go", term, root],
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                    for line in result.stdout.splitlines():
                        p = Path(line.strip())
                        if not p.exists() or "_test.go" in p.name:
                            continue
                        hits[p] = hits.get(p, 0) + 2
                except subprocess.TimeoutExpired:
                    pass

        for err in error_strings:
            log.debug(f"grep error: {err!r}")
            for root in roots:
                try:
                    result = subprocess.run(
                        ["grep", "-rl", "--include=*.go", err, root],
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                    for line in result.stdout.splitlines():
                        p = Path(line.strip())
                        if p.exists() and "_test.go" not in p.name:
                            hits[p] = hits.get(p, 0) + _ERROR_HIT_BONUS
                except subprocess.TimeoutExpired:
                    pass

        return list(hits.items())

    def _sibling_test_files(self, impl: Path, grep_terms: list[str]) -> list[Path]:
        """Tests in the same directory as an implementation hit."""
        tests: list[Path] = []
        directory = impl.parent
        paired = directory / f"{impl.stem}_test.go"
        if paired.is_file():
            tests.append(paired)

        for test_path in sorted(directory.glob("*_test.go")):
            if test_path == paired:
                continue
            if grep_terms:
                try:
                    text = test_path.read_text(errors="replace")
                    if not any(t in text for t in grep_terms):
                        continue
                except OSError:
                    continue
            tests.append(test_path)

        return tests

    def _check_crg(self) -> bool:
        try:
            subprocess.run(
                ["code-review-graph", "--version"],
                capture_output=True,
                timeout=5,
            )
            return True
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    def _expand_with_crg(self, candidate_files: list[Path], issue: dict) -> list[Path]:
        try:
            subprocess.run(
                ["code-review-graph", "build"],
                cwd=self.repo,
                capture_output=True,
                timeout=60,
            )
            if not candidate_files:
                return candidate_files

            changed_arg = ",".join(
                str(f.relative_to(self.repo)) for f in candidate_files
            )
            result = subprocess.run(
                [
                    "code-review-graph",
                    "detect-changes",
                    "--files",
                    changed_arg,
                    "--format",
                    "json",
                ],
                cwd=self.repo,
                capture_output=True,
                text=True,
                timeout=30,
            )

            if result.returncode == 0 and result.stdout.strip():
                data = json.loads(result.stdout)
                blast_files = data.get("affected_files", [])
                expanded = list(candidate_files)
                for bf in blast_files:
                    p = self.repo / bf
                    if p.exists() and p not in expanded:
                        expanded.append(p)
                return expanded[:_MAX_TOTAL_FILES]
        except Exception:
            pass
        return candidate_files

    def _extract_candidate_functions(
        self, files: list[Path], search_terms: list[str]
    ) -> list[dict]:
        import re

        functions = []
        for fpath in files:
            try:
                source = fpath.read_text(errors="replace")
                file_functions = self._parse_go_functions(source, fpath)
                for fn in file_functions:
                    score = 0
                    for term in search_terms:
                        if term.lower() in fn["name"].lower():
                            score += 2
                        if term in fn["body"]:
                            score += 1
                    fn["score"] = score
                    functions.append(fn)
            except Exception:
                pass

        functions.sort(key=lambda f: f["score"], reverse=True)
        return functions[:6]

    def _parse_go_functions(self, source: str, fpath: Path) -> list[dict]:
        import re

        lines = source.splitlines()
        functions = []
        func_header_re = re.compile(
            r"^func\s+(?:\([^)]+\)\s+)?(\w+)\s*\([^)]*\)[^{]*\{"
        )

        i = 0
        while i < len(lines):
            line = lines[i]
            m = func_header_re.match(line.strip())
            if m:
                func_name = m.group(1)
                start_line = i
                depth = 0
                end_line = i
                for j in range(i, min(i + 200, len(lines))):
                    depth += lines[j].count("{") - lines[j].count("}")
                    if depth <= 0 and j > i:
                        end_line = j
                        break
                else:
                    end_line = min(i + 100, len(lines) - 1)

                body_lines = lines[start_line : end_line + 1]
                functions.append({
                    "name": func_name,
                    "file": str(fpath.relative_to(self.repo)),
                    "abs_file": str(fpath),
                    "start_line": start_line + 1,
                    "end_line": end_line + 1,
                    "signature": lines[start_line].strip(),
                    "body": "\n".join(body_lines),
                    "neighbor_signatures": [],
                })
                i = end_line + 1
            else:
                i += 1

        return functions

    def _attach_neighbor_signatures(self, functions: list[dict]) -> list[dict]:
        import re

        sig_map = self._build_signature_map()
        for fn in functions:
            called = re.findall(r"\b([A-Z][a-zA-Z0-9]*)\s*\(", fn["body"])
            called += re.findall(r"\.([\w]+)\s*\(", fn["body"])
            called = list(set(called))
            neighbor_sigs = []
            for callee_name in called:
                if callee_name in sig_map and callee_name != fn["name"]:
                    neighbor_sigs.append(sig_map[callee_name])
            fn["neighbor_signatures"] = neighbor_sigs[:10]
        return functions

    def _build_signature_map(self) -> dict[str, str]:
        import re

        sig_map = {}
        func_re = re.compile(
            r"^func\s+(?:\([^)]+\)\s+)?(\w+)\s*(\([^)]*\)[^{]*?)(?:\{|$)"
        )
        for go_file in self.repo.rglob("*.go"):
            if "vendor" in go_file.parts:
                continue
            try:
                for line in go_file.read_text(errors="replace").splitlines():
                    m = func_re.match(line.strip())
                    if m:
                        name = m.group(1)
                        sig_map[name] = (
                            f"// {go_file.relative_to(self.repo)}\n"
                            f"{line.strip().rstrip('{').strip()}"
                        )
            except Exception:
                pass
        return sig_map

    def _scoped_test_files(self, impl_file: Path) -> list[Path]:
        directory = impl_file.parent
        paired = directory / f"{impl_file.stem}_test.go"
        tests = [paired] if paired.is_file() else []
        tests.extend(
            p for p in sorted(directory.glob("*_test.go")) if p not in tests
        )
        return tests[:3]

    def _extract_conventions(self, functions: list[dict]) -> str:
        import re

        if not functions:
            return "Standard Go conventions"

        main_file = Path(functions[0]["abs_file"])
        try:
            source = main_file.read_text(errors="replace")
        except Exception:
            return "Standard Go conventions"

        conventions = []
        if "fmt.Errorf" in source:
            conventions.append("Error wrapping: uses fmt.Errorf with %w verb")
        elif "errors.New" in source:
            conventions.append("Error creation: uses errors.New")

        test_files = self._scoped_test_files(main_file)
        if test_files:
            try:
                test_src = test_files[0].read_text(errors="replace")
                if "TestMain" in test_src:
                    conventions.append("Tests: uses TestMain for setup")
                if "t.Run" in test_src:
                    conventions.append("Tests: uses t.Run for subtests")
                if "assert." in test_src or "require." in test_src:
                    conventions.append("Tests: uses testify assert/require")
            except Exception:
                pass

        receiver_re = re.compile(r"func\s+\((\w+)\s+\*?\w+\)")
        receivers = receiver_re.findall(source)
        if receivers:
            short = [r for r in receivers if len(r) == 1]
            if len(short) > len(receivers) / 2:
                conventions.append("Receivers: short single-letter names")

        if "return nil, err" in source:
            conventions.append("Error returns: (result, error) tuple pattern")

        return (
            "\n".join(f"- {c}" for c in conventions)
            if conventions
            else "Standard Go conventions"
        )

    def _build_compact_tree(self) -> str:
        import re

        lines = []
        pkg_re = re.compile(r"^package\s+(\w+)")
        for go_file in sorted(self.repo.rglob("*.go")):
            if "vendor" in go_file.parts:
                continue
            rel = go_file.relative_to(self.repo)
            pkg = ""
            try:
                for line in go_file.read_text(errors="replace").splitlines()[:5]:
                    m = pkg_re.match(line.strip())
                    if m:
                        pkg = m.group(1)
                        break
            except Exception:
                pass
            lines.append(f"{rel}  [pkg:{pkg}]")
        return "\n".join(lines[:60])

    def _assemble_context_string(
        self, functions: list[dict], conventions: str, tree: str, issue: dict
    ) -> str:
        u = issue.get("understanding") or {}
        parts = [
            "## Issue intake (for scope)\n"
            f"- **Type:** {u.get('type', 'unknown')}\n"
            f"- **Symptom:** {u.get('symptom', issue.get('title', ''))}\n"
            f"- **Expected:** {u.get('expected', 'unknown')}\n"
            f"- **Actual:** {u.get('actual', 'unknown')}\n",
            format_conventions_block(conventions),
        ]
        parts.append("\n## Relevant function bodies\n")
        for fn in functions:
            parts.append(
                f"### {fn['name']} ({fn['file']}, lines {fn['start_line']}-{fn['end_line']})\n"
                f"```go\n{fn['body']}\n```"
            )

        if any(fn["neighbor_signatures"] for fn in functions):
            parts.append("\n## Neighbor signatures (callers/callees — no bodies)\n")
            seen = set()
            for fn in functions:
                for sig in fn["neighbor_signatures"]:
                    if sig not in seen:
                        parts.append(f"```go\n{sig}\n```")
                        seen.add(sig)

        return "\n".join(parts)
