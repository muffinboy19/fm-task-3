"""
Plan adherence checker (used by Validator).

Verifies the generated patch matches the fix plan from step 3.
"""

import json
import re
from typing import Any, Optional

from modules.llm import LLMClient, load_prompt


def files_in_patch(patch: str) -> list[str]:
    paths: list[str] = []
    for line in patch.splitlines():
        if not line.startswith("diff --git"):
            continue
        parts = line.split()
        if len(parts) >= 4 and parts[3].startswith("b/"):
            paths.append(parts[3][2:])
    return sorted(set(paths))


def _heuristic_file_overlap(plan: str, patch: str) -> dict[str, Any]:
    patch_files = files_in_patch(patch)
    # Paths mentioned in plan (backticks, table cells, bullets)
    candidates = set(
        re.findall(r"`([a-zA-Z0-9_./-]+\.go)`", plan)
        + re.findall(r"\|\s*`?([a-zA-Z0-9_./-]+\.go)`?\s*\|", plan)
        + re.findall(r"(?:^|\s)([a-zA-Z0-9_./-]+\.go)(?:\s|$)", plan, re.MULTILINE)
    )
    planned = sorted(candidates)
    planned_set = set(planned)
    patch_set = set(patch_files)
    return {
        "planned_files_heuristic": planned,
        "patch_files": patch_files,
        "missing_from_patch_heuristic": sorted(planned_set - patch_set),
        "extra_in_patch_heuristic": sorted(patch_set - planned_set),
    }


def _parse_llm_json(text: str) -> dict[str, Any]:
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        raise ValueError("Plan checker did not return JSON")
    return json.loads(m.group(0))


class PlanChecker:
    def __init__(self, api_key: Optional[str] = None):
        self.llm = LLMClient(api_key=api_key)

    def check(self, plan: str, patch: str) -> dict[str, Any]:
        heuristic = _heuristic_file_overlap(plan, patch)
        system = load_prompt("plan_check.txt")
        user = f"""## Fix plan

{plan[:12000]}

---

## Unified diff

```diff
{patch[:16000]}
```

Heuristic file lists (for reference):
- patch_files: {heuristic["patch_files"]}
- planned_files_heuristic: {heuristic["planned_files_heuristic"]}
- missing_from_patch_heuristic: {heuristic["missing_from_patch_heuristic"]}
- extra_in_patch_heuristic: {heuristic["extra_in_patch_heuristic"]}

Compare the plan and patch. Output JSON only.
"""
        raw = self.llm.complete(
            system=system, user=user, max_tokens=2048, module="plan_checker"
        )
        result = _parse_llm_json(raw)
        result["heuristic"] = heuristic
        result["patch_files"] = result.get("patch_files") or heuristic["patch_files"]
        if not result.get("planned_files"):
            result["planned_files"] = heuristic["planned_files_heuristic"]
        return result
