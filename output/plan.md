# Fix plan

## What we'll do
- Add `(*Validate) JSONSchema(s interface{}) ([]byte, error)` to emit JSON Schema (draft-07) for a struct type used as config
- Reuse existing tag parsing via `extractStructCache` and walk each field’s `cTag` chain (same data validation uses)
- Map a v1 subset of `validate` tags to schema keywords (`required`, `min`/`max`/`len`, `oneof`, `email`/`url`/`uuid`, `dive`→`items`, nested `properties`)
- Resolve JSON property names with `RegisterTagNameFunc` when set, else `json` tag (first segment, skip `-`), else struct field name
- Document unsupported tags (cross-field `required_*`/`excluded_*`, struct-level funcs, custom validators) and point to invopop/jsonschema for full metadata
- Add `doc.go` section with a minimal config-struct example

## Files to change
- `jsonschema.go` — reflect walk, `cTag`→schema mapper, `encoding/json` marshal
- `jsonschema_test.go` — table-driven `t.Run` subtests with assert + `json.Unmarshal` round-trip checks
- `doc.go` — public API note, supported tags, limitations

## Tests
- `jsonschema_test.go` — `TestJSONSchema_required`: `validate:"required"` adds property to top-level `required`
- `jsonschema_test.go` — `TestJSONSchema_stringConstraints`: `min`/`max`/`len` become `minLength`/`maxLength` on strings
- `jsonschema_test.go` — `TestJSONSchema_oneof`: `oneof=a b c` becomes `enum`
- `jsonschema_test.go` — `TestJSONSchema_formats`: `email`/`url`/`uuid` map to `format`
- `jsonschema_test.go` — `TestJSONSchema_nestedStruct`: nested struct becomes `properties` + `type: object`
- `jsonschema_test.go` — `TestJSONSchema_dive`: `dive` on slice sets `items` schema from element rules
- `jsonschema_test.go` — `TestJSONSchema_jsonPropertyNames`: `json` tag names used when no `RegisterTagNameFunc`
- `jsonschema_test.go` — `TestJSONSchema_skipTag`: `validate:"-"` omits field from schema
- `jsonschema_test.go` — `TestJSONSchema_alias`: `RegisterAlias` expands to same constraints as target tags

## Assumptions
- Feature ships in core `validator` package (no `invopop/jsonschema` dependency); invopop remains optional for titles/descriptions/examples
- v1 covers scalar, slice (`dive`), and nested struct fields only; map `keys`/`endkeys` and cross-field conditional tags are out of scope
- Draft-07 is the target JSON Schema version for stable, widely supported config tooling