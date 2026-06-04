## Root cause

`go-playground/validator` only compiles and executes `validate` tags at runtime (`extractStructCache` / `parseFieldTagsRecursive` in `cache.go`). There is no API that introspects those tags and emits JSON Schema. Issue [#1518](https://github.com/go-playground/validator/issues/1518) asks for schema generation for config files; a maintainer noted this is currently handled externally (e.g. [invopop/jsonschema](https://github.com/invopop/jsonschema)), which reads `json` tags—not `validate` tags—so it does not reflect validator constraints today.

## Proposed fix

Add an **opt-in JSON Schema generator in the root `validator` package** (not core validation path) that reuses the existing tag cache and alias resolution:

1. **Public entry point** on `*Validate`:
   - `JSONSchema(i any) ([]byte, error)` — accepts a struct instance or pointer (same shape as `Struct`).
   - Optional companion: `JSONSchemaFromType(reflect.Type) (map[string]any, error)` for tooling that only has a type.

2. **Internal generator** (new unexported helper, e.g. `generateJSONSchema(v *Validate, typ reflect.Type) map[string]any`):
   - Bootstrap struct metadata via existing `extractStructCache(reflect.Zero(typ), typ.Name())` so aliases, custom tag name func, and `rules` overrides are honored.
   - Walk `cField` / `cTag` chains already parsed for validation.
   - Build JSON Schema Draft 2020-12 (`$schema`, `type`, `properties`, `required`, `items`, etc.) as `map[string]any`, then `json.Marshal` in `JSONSchema`.

3. **Field naming** (config-file oriented):
   - Property key from `json` tag (strip `,omitempty`), skip `json:"-"`.
   - If `RegisterTagNameFunc` is set, use it (same precedence as validation namespaces).
   - Fall back to struct field name.

4. **Type mapping** from `reflect.Type` / `Kind`:
   - `string` → `"string"`; numeric kinds → `"integer"` or `"number"`; `bool` → `"boolean"`.
   - `slice`/`array` → `"array"` + `"items"` (follow `dive` into element schema).
   - `map` → `"object"` (values from `dive`; keys as `"additionalProperties"` unless `keys`/`endkeys` present—see assumptions).
   - nested struct → nested `"object"` with `"properties"`.
   - `time.Time` → `"string"` with `"format": "date-time"`.

5. **Constraint mapping** (first pass—common config tags only):

   | `validate` tag | JSON Schema keyword |
   |---|---|
   | `required` | field in parent `"required"` |
   | `min` / `max` | `minimum`/`maximum` (numbers) or `minLength`/`maxLength` (string/array) |
   | `len=n` | `minLength`/`maxLength` = n |
   | `gt`/`gte`/`lt`/`lte` | `exclusiveMinimum`/`minimum`/`exclusiveMaximum`/`maximum` |
   | `oneof` | `enum` |
   | `email`, `url`, `uri`, `uuid`, `ipv4`, `ipv6` | `format` |
   | `regexp`/`contains`/`startswith`/… | `pattern` where a stable regex exists in `regexes.go` |
   | `-`, `structonly`, `omitempty`, `omitzero`, `omitnil` | omit from `"required"` / no extra keywords |
   | cross-field (`eqfield`, `required_with`, …) | **skip** (not expressible in per-field schema) |

6. **Documentation only in README**: short “JSON Schema generation” section with example and explicit limitations (cross-field rules, custom validators). No change to validation behavior.

**Scope guard:** Do not add `invopop/jsonschema` as a dependency; emit plain `encoding/json`-friendly maps to keep the module lean and aligned with maintainer feedback.

## Files to change

| File | Change |
|---|---|
| `jsonschema.go` | New: `JSONSchema`, `JSONSchemaFromType`, schema builder walking `cStruct`/`cField`/`cTag`, tag→keyword mapping, property/required assembly |
| `jsonschema_test.go` | New: table-driven tests (see below) |
| `README.md` | New subsection under Usage: feature, example, supported tags, limitations |

**No changes** to `cache.go`, `validator.go`, `baked_in.go`, or validation hot paths unless a tiny exported helper is needed (prefer reusing existing unexported cache from the same package).

## Test strategy

Add `jsonschema_test.go` in package `validator`, using dot-imported `github.com/go-playground/assert/v2` and `t.Run` subtests like sibling tests.

| Test | Cases | Expected |
|---|---|---|
| `TestJSONSchema_simpleConfig` | Struct with `Name string \`json:"name" validate:"required,min=1,max=64"\``, `Port int \`json:"port" validate:"required,gte=1,lte=65535"\``, `Debug bool \`json:"debug"\`` | Unmarshal output; `type=object`; `properties.name` has `type=string`, `minLength=1`, `maxLength=64`; `properties.port` has `minimum=1`, `maximum=65535`; `required` contains `"name"`, `"port"`; `"debug"` not required |
| `TestJSONSchema_omitemptyNotRequired` | `Opt string \`json:"opt" validate:"omitempty,min=1"\`` | Field absent from `required`; still has `minLength=1` when present |
| `TestJSONSchema_skipDashTag` | `Skip string \`json:"skip" validate:"-"\`` | Property omitted entirely |
| `TestJSONSchema_jsonDashOmitted` | `Hidden string \`json:"-" validate:"required"\`` | Property omitted |
| `TestJSONSchema_oneofEnum` | `Mode string \`json:"mode" validate:"oneof=dev prod"\`` | `properties.mode.enum` == `["dev","prod"]` |
| `TestJSONSchema_nestedStruct` | Parent with nested struct field `validate:"required"` | Child schema under `properties.<field>.properties`; child required fields propagated |
| `TestJSONSchema_diveSlice` | `Tags []string \`json:"tags" validate:"dive,min=1"\`` | `type=array`, `items.type=string`, `items.minLength=1` |
| `TestJSONSchema_registerTagNameFunc` | Same as `TestCustomFieldName` pattern using `schema` tag + `RegisterTagNameFunc` | Property keys use custom names (`b`, `c`), not Go field names |
| `TestJSONSchema_nonStructInput` | Pass `validate.Var("x", "required")` input (string) | Returns error (mirrors `Struct` misuse semantics) |
| `TestJSONSchema_roundTripMarshal` | Any valid schema from above | `json.Unmarshal` into `map[string]any` succeeds; top-level `$schema` present |

**Before fix:** all new tests fail (symbols/method missing). **After fix:** `go test -run TestJSONSchema ./...` passes.

## Risks / assumptions

- **Maintainer scope:** Core team may prefer closing as “use invopop/jsonschema.” This plan treats the issue as “schema from `validate` tags,” which external tools do not provide; implementation stays isolated in new files.
- **Partial tag coverage:** Only commonly mappable baked-in tags in v1; cross-field, conditional (`required_if`, `required_with`), struct-level validators, and user-registered validators are documented as unsupported (no panic, no false constraints).
- **JSON Schema draft:** Assume Draft 2020-12 (`$schema: https://json-schema.org/draft/2020-12/schema`); document if a different draft is chosen.
- **Map `keys`/`endkeys`:** v1 assumes string-keyed maps with `dive` on values only; keyed map validation rules are out of scope unless explicitly added later.
- **Pointer fields:** Treat non-nil struct pointers as nested objects; pointer optionality vs `required` follows `validate` tags on the pointer field, not Go nilability alone.
- **Performance:** Acceptable for offline config schema export; no caching beyond existing `structCache` reuse.
- **API stability:** New exported methods only; no breaking changes to v10 validation API.