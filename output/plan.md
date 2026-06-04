## Root cause

The library validates values at runtime from `validate` struct tags but exposes no API to emit JSON Schema. Issue [#1518](https://github.com/go-playground/validator/issues/1518) asks for schema generation for config files; there is no `jsonschema` code in the repo today. A maintainer comment ([Jan 2026](https://github.com/go-playground/validator/issues/1518#issuecomment)) argues this should not live in the core `validator` package and points users to [invopop/jsonschema](https://github.com/invopop/jsonschema), which reads `json` tags—not `validate` tags—unless callers add custom interceptors.

Tag parsing and struct walking exist only as unexported logic in `cache.go` (`extractStructCache`, `parseFieldTagsRecursive`). There is no exported introspection surface, so a schema generator cannot reuse the validator’s cached tag plan without new code or API.

## Proposed fix

Add an **opt-in subpackage** `non-standard/jsonschema` (same pattern as `non-standard/validators`), **not** core `validator` API.

1. **Public entry points** (names aligned with sibling packages):
   - `Generate(v any, opts ...Option) ([]byte, error)` — marshaled JSON Schema document.
   - `GenerateSchema(v any, opts ...Option) (map[string]any, error)` — same tree, for tests/custom formatting.

2. **Options** (functional options like `options.go`):
   - `WithJSONTag()` — default on: property names from `json` tag (first segment before `,`; skip `-` and `omitempty` for naming only).
   - `WithTagName(name string)` — default `"validate"`; mirror `Validate.SetTagName`.
   - `WithRequiredStructEnabled()` — align with `WithRequiredStructEnabled`: non-pointer struct fields with `required` are required in schema.
   - `WithDraft(version string)` — set root `$schema` (default Draft 2020-12 URI).

3. **Reflection walker** (new, read-only; do not call `Validate.Struct`):
   - Walk exported struct fields (skip unexported unless future opt-in; skip `validate:"-"`).
   - Map Go kinds → JSON Schema `type` (`string`, `integer`/`number`, `boolean`, `object`, `array`).
   - Pointers: `nullable` when field is not required; required pointer still listed in parent `required`.
   - `dive`: nested struct → `properties`; slice/array → `items`; map → `additionalProperties` (keys not validated in schema—document limitation).
   - **Tag → schema mapping (MVP, config-oriented):**

   | `validate` tag | JSON Schema |
   |----------------|-------------|
   | `required` | parent `required` |
   | `omitempty`, `omitnil`, `omitzero` | omit from `required` (field stays in `properties`) |
   | `min`, `max`, `len`, `eq` | `minLength`/`maxLength` (string/[]byte) or `minimum`/`maximum`/`minItems`/`maxItems` by kind |
   | `gt`, `gte`, `lt`, `lte` | `exclusiveMinimum`/`minimum`/`exclusiveMaximum`/`maximum` |
   | `oneof` | `enum` (split param on spaces) |
   | `email`, `url`, `uri`, `uuid`, `uuid4`, `ipv4`, `ipv6` | `format` |
   | `unique` | `uniqueItems: true` |
   | `boolean` | `type: boolean` on string fields (if used) |

   - **Explicitly not mapped in v1** (no error; optional `description` comment in code/docs only): cross-field (`eqfield`, `required_without`, …), `keys`/`endkeys`, `or`/`|`, struct-level validators, custom `RegisterValidation` tags, aliases unless duplicated in a small local alias table (`iscolor` → formats). Unknown tags are ignored.

4. **Lightweight tag parser** in `tags.go`: copy splitting rules from `cache.go` (`tagSeparator`, `tagKeySeparator`, `dive`, `keys`/`endkeys` handling for traversal only). Do **not** import or export unexported `cTag` machinery.

5. **Docs / discoverability** (minimal):
   - `_examples/jsonschema/main.go` — generate schema for a nested config struct, print JSON.
   - Short README bullet under non-standard or a subsection in main README linking to the subpackage and noting invopop for advanced/custom rules.

**Alternative (lower scope, maintainer-aligned):** docs + example only, using invopop with `InterceptProp` reading `validate` tags—does not add generation inside this repo; treat as fallback if maintainers reject any new code.

## Files to change

| File | Change |
|------|--------|
| `non-standard/jsonschema/jsonschema.go` | `Generate`, `GenerateSchema`, `Option`, root `$schema` / `type: object` |
| `non-standard/jsonschema/reflect.go` | struct/slice/map/pointer walk, `properties` / `items` / `additionalProperties` |
| `non-standard/jsonschema/tags.go` | parse `validate` tag string → constraint structs |
| `non-standard/jsonschema/jsonschema_test.go` | table-driven tests (see below) |
| `_examples/jsonschema/main.go` | runnable example |
| `README.md` | one-line feature pointer + link to subpackage (optional but recommended) |

**No changes** to `validator.go`, `cache.go`, `baked_in.go`, or `validator_test.go` unless later exporting shared tag parsing is explicitly approved (avoid for minimal diff).

## Test strategy

All tests in `non-standard/jsonschema/jsonschema_test.go`, package `jsonschema`, using `github.com/go-playground/assert/v2` like `notblank_test.go`. Use table tests + `t.Run` where multiple cases share setup. Assert via `json.Unmarshal` into `map[string]any` and deep checks on nested keys (not exact byte strings, unless stable ordering is guaranteed).

| Test | Cases | Expected |
|------|--------|----------|
| `TestGenerate_simpleStruct` | struct with `validate:"required,email"`, numeric `gte`/`lte` | Root `type: object`; property in `required`; string field `format: email`; numeric bounds set |
| `TestGenerate_jsonTagNames` | `json:"e-mail"` + `validate:"required"` | Property key `e-mail`, not Go field name |
| `TestGenerate_skipField` | `validate:"-"` | Field absent from `properties` |
| `TestGenerate_omitemptyNotRequired` | `json:"x,omitempty"` + `validate:"omitempty"` | `x` in `properties`, not in `required` |
| `TestGenerate_nestedStructDive` | embedded struct with `dive` | Nested `properties`; child `required` respected |
| `TestGenerate_sliceDive` | `[]T` with `dive,required` | `type: array`, `items` schema, `minItems` if `gt=0` present |
| `TestGenerate_mapDive` | `map[string]T` with `dive` | `additionalProperties` schema for value type |
| `TestGenerate_oneof` | `validate:"oneof=a b c"` | `enum: ["a","b","c"]` |
| `TestGenerate_pointerNullable` | `*string` without `required` | `type: string`, `nullable: true` (or oneOf null+string—pick one style and lock in tests) |
| `TestGenerate_unsupportedCrossFieldIgnored` | `required_without=Other` | Schema still generated; no `dependentRequired` in v1 |
| `TestGenerate_nonStructError` | pass `int` | non-nil `error` |

**Failing-before / passing-after:** add tests first against empty/stub `Generate` (compile with `panic("todo")` or returning `{}`); `go test ./non-standard/jsonschema/...` fails; implement walker/mapping until green. Full suite: `go test ./...` from repo root.

## Risks / assumptions

- **Assumption:** “Generate JSON Schema from struct tags” means **document the JSON shape implied by `json` + `validate` tags** for config files, not a byte-for-byte encoding of every validator rule.
- **Assumption:** Draft **2020-12** default; consumers needing Draft-04 should use `WithDraft` or post-process.
- **Assumption:** Property names come from **`json` tags** when present (config-file use case); Go field names only as fallback—matches `RegisterTagNameFunc` examples in tests.
- **Risk:** Schema/validation **drift** for unmapped tags (cross-field, conditional required, custom validators)—document in README; users needing full fidelity should combine with invopop or hand-maintain extensions.
- **Risk:** Maintainers may prefer **docs-only** resolution; subpackage is the compromise to satisfy the issue without bloating core `validator`.
- **Risk:** Duplicated tag parsing may diverge from `parseFieldTagsRecursive` for aliases/`or`/`|`; v1 does not promise parity—only covered tags in tests are guaranteed.
- **No new runtime dependency** in `go.mod` for MVP (stdlib `encoding/json` + `reflect` only); adding invopop is optional later for delegation, not required for the minimal fix.