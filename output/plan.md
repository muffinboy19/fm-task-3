## Root cause

The library validates values at runtime via `validate` struct tags (parsed in `extractStructCache` / `parseFieldTagsRecursive` in `cache.go`) but has no code path that emits JSON Schema. Issue [#1518](https://github.com/go-playground/validator/issues/1518) asks for the inverse: static schema generation for config/documentation. A maintainer already noted this likely should not live in core and pointed to [invopop/jsonschema](https://github.com/invopop/jsonschema), which generates schema from `json` / `jsonschema` tags—not from `validate` tags—so today users must duplicate constraints or hand-bridge tags themselves.

## Proposed fix

**Recommended (minimal, aligned with maintainer feedback):** Do not add schema generation to the main `validator` package. Add a small **optional** module `jsonschema/` (own `go.mod`, same pattern as `_examples/validate_fn`) that wraps `invopop/jsonschema` and maps a **bounded subset** of `validate` tags onto JSON Schema keywords via `Reflector.InterceptProp`. Expose one entry point:

- `Reflect(cfg any, opts ...Option) (*jsonschema.Schema, error)` — uses `json` tag names for properties; optional `Option` to mirror `RegisterTagNameFunc` (e.g. custom name from `schema` tag, same idea as `TestCustomFieldName`).

**Phase 1 tag mapping** (everything else ignored silently or documented as unsupported):

| `validate` | JSON Schema |
|------------|-------------|
| `required` | add to parent `required` |
| `-` | omit property |
| `min` / `max` | `minimum`/`maximum` or `minLength`/`maxLength` by Go kind |
| `len=N` | `minLength` & `maxLength` = N |
| `eq=val` | `const` |
| `oneof=a b c` | `enum` |
| `email`, `url`, `uri`, `http_url`, `https_url` | `format` |
| `uuid`, `uuid4`, … | `pattern` (reuse regex intent from `baked_in.go` where practical) |
| `numeric`, `number`, `boolean` | tighten `type` |
| `dive,...` | `items` sub-schema from remainder of tag chain |
| nested struct / pointer to struct | `$ref` or inline `properties` via reflect walk |

**Out of scope for v1:** cross-field tags (`eqfield`, `gtfield`, …), conditional tags (`required_if`, `excluded_*`, …), `structonly`/`keys`/`endkeys` map-key rules, and custom `RegisterValidation` tags (no stable schema semantics).

**Docs:** Short README section + `_examples/jsonschema/main.go` showing `Reflect` → `json.MarshalIndent` to a config schema file.

**Not proposed:** Exporting internal `cStruct`/`cTag` cache, or mapping all ~100+ baked-in validators in the first PR.

## Files to change

| File | Change |
|------|--------|
| `jsonschema/go.mod` | New module; `require github.com/invopop/jsonschema` and `github.com/go-playground/validator/v10` |
| `jsonschema/reflect.go` | `Reflect`, options (`WithTagNameFunc`, `WithValidateTagName`), struct walk + `InterceptProp` hook |
| `jsonschema/mapping.go` | `applyValidateTag(chain, prop *jsonschema.Schema, parent *jsonschema.Schema, name string)` for Phase 1 table |
| `jsonschema/reflect_test.go` | All new tests (package `jsonschema_test` or `jsonschema` per sibling style in `non-standard/validators`) |
| `_examples/jsonschema/main.go` | Runnable example writing schema JSON |
| `README.md` | “JSON Schema generation” subsection linking to `jsonschema/` module and example |
| `doc.go` | One paragraph cross-link (no API in root `validator` package) |

**No changes** to `cache.go`, `validator_instance.go`, `baked_in.go`, or `validator_test.go` unless a later PR adds exported introspection (not needed for v1).

## Test strategy

Add `jsonschema/reflect_test.go` using `github.com/go-playground/assert/v2` and table-driven `t.Run` (match `non-standard/validators/notblank_test.go` style). Assert via `json.Marshal` + `json.Unmarshal` into `map[string]any` and check keys (same round-trip style used elsewhere in the repo).

| Test | Cases | Expected |
|------|-------|----------|
| `TestReflect_required` | `struct { Name string \`json:"name" validate:"required"\` }` | `properties.name` present; root `required` contains `"name"` |
| `TestReflect_minMax_string` | `Bio string \`validate:"min=2,max=10"\`` | `minLength: 2`, `maxLength: 10` |
| `TestReflect_len` | `Code string \`validate:"len=4"\`` | `minLength` and `maxLength` both 4 |
| `TestReflect_oneof` | `Role string \`validate:"oneof=admin user"\`` | `enum`: `["admin","user"]` |
| `TestReflect_email` | `Email string \`validate:"email"\`` | `format`: `"email"` |
| `TestReflect_nested` | nested struct with inner `validate:"required"` | inner object in `properties`; inner field in nested `required` |
| `TestReflect_dive` | `Tags []string \`validate:"dive,required"\`` | `type: array`, `items.required` or equivalent item constraints |
| `TestReflect_skip` | `Secret string \`validate:"-"\`` | property absent from `properties` |
| `TestReflect_customTagName` | `RegisterTagNameFunc` / `WithTagNameFunc` using `schema:"b"` | property key `"b"`, not `"B"` |
| `TestReflect_unsupportedTag` | `validate:"eqfield=Other"` | schema emitted without `const`/cross-field noise; no panic |
| `TestReflect_marshalRoundTrip` | simple config struct | `Reflect` → marshal → unmarshal succeeds; stable `type: object` at root |

**Failing-before / passing-after:** Run `go test ./jsonschema/...` on current `master` — module absent, so tests do not exist (N/A). After implementation, tests fail if mapping is wrong, pass when Phase 1 table is implemented.

**Regression:** `go test ./...` at repo root must remain green (core module unchanged).

## Risks / assumptions

- **Assumption:** Acceptable deliverable is an **optional** `jsonschema/` module + docs, not a new method on `validator.Validate`, per [maintainer comment](https://github.com/go-playground/validator/issues/1518#issuecomment-3741124446).
- **Assumption:** Users want property names from **`json` tags** (default); `validate` tags only drive constraints, matching how configs are serialized.
- **Risk:** JSON Schema cannot express many validator rules (conditional required, cross-field, filesystem `file`/`dir`, custom validators)—generated schema may be **weaker** than runtime validation; document “schema is indicative, not equivalent to `Struct()`”.
- **Risk:** Alias expansion (`RegisterAlias`, baked-in aliases like `iscolor`) must be expanded before mapping or users get incomplete schemas; v1 should reuse the same comma-split + alias resolution rules as `parseFieldTagsRecursive` (copied minimally into `jsonschema`, not imported from unexported internals).
- **Risk:** Adding `invopop/jsonschema` to the **root** `go.mod` would affect all consumers; keep it in a **separate** `jsonschema/go.mod` unless maintainers explicitly want a first-class subpackage in the main module.
- **Alternative rejected for minimal scope:** Full in-core `(*Validate).JSONSchema()` with complete baked-in tag coverage—too large, duplicates invopop, conflicts with maintainer guidance.