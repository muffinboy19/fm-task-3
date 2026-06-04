# Add JSON Schema generation from validate struct tags

## Summary

Users need a way to produce JSON Schema documents for complex configuration structs without maintaining a separate schema by hand. This PR adds `(*Validate).JSONSchema`, which emits draft-07 JSON Schema by walking the same `cTag` chains and struct cache that runtime validation uses. Property names follow `RegisterTagNameFunc` when set, otherwise the `json` tag (first segment, skipping `-`), otherwise the Go field name.

## Changes

- Add `(*Validate) JSONSchema(s interface{}) ([]byte, error)` returning draft-07 JSON with `$schema` set
- Walk struct fields via `extractStructCache` and map a v1 subset of `validate` tags to schema keywords (`required`, `min`/`max`/`len`, `oneof`, `email`/`url`/`uuid`, numeric `minimum`/`maximum`, `dive` → `items`, nested `properties`)
- Support nested structs, slices/arrays, pointers, and `time.Time` as string
- Honor `validate:"-"` to omit fields; respect `RegisterAlias` when expanding tag chains
- Document the API, supported tags, and limitations in `doc.go` (cross-field conditionals, struct-level/custom validators, map `keys`/`endkeys` out of scope; point to `github.com/invopop/jsonschema` for titles/descriptions/examples)
- Add table-driven tests with JSON unmarshal/marshal round-trip checks

## Test plan

- [x] `go test -run 'TestJSONSchema' -v` — all 10 subtests pass
- [x] `TestJSONSchema_required` — `required` adds property to top-level `required`
- [x] `TestJSONSchema_stringConstraints` — `min`/`max`/`len` → `minLength`/`maxLength` on strings
- [x] `TestJSONSchema_oneof` — `oneof=a b c` → `enum`
- [x] `TestJSONSchema_formats` — `email`/`url`/`uuid` → `format`
- [x] `TestJSONSchema_nestedStruct` — nested struct → `type: object` with `properties`
- [x] `TestJSONSchema_dive` — `dive` on slice sets `items` from element rules
- [x] `TestJSONSchema_jsonPropertyNames` — `json` tag names when no `RegisterTagNameFunc`
- [x] `TestJSONSchema_skipTag` — `validate:"-"` omits field
- [x] `TestJSONSchema_alias` — `RegisterAlias` produces same constraints as target tags
- [x] `TestJSONSchema_invalidInput` — non-struct input returns `InvalidValidationError`

## Closes

https://github.com/go-playground/validator/issues/1518