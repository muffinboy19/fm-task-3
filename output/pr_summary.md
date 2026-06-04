# Add JSON Schema generation from validate struct tags

## Summary

`go-playground/validator` compiles and enforces `validate` tags only at runtime; there is no way to emit a schema that reflects those constraints for offline config validation or documentation ([#1518](https://github.com/go-playground/validator/issues/1518)). External generators such as [invopop/jsonschema](https://github.com/invopop/jsonschema) read `json` tags instead, so they do not mirror validator rules.

This PR adds an opt-in JSON Schema Draft 2020-12 generator on `*Validate` that reuses the existing struct tag cache (`extractStructCache`) and walks parsed `cField` / `cTag` chains to build schema documents from the same tags used for validation.

## Changes

- Add `(*Validate).JSONSchema(i any) ([]byte, error)` — accepts a struct instance or pointer (same shape as `Struct`) and returns marshaled JSON Schema.
- Add `(*Validate).JSONSchemaFromType(typ reflect.Type) (map[string]any, error)` — type-only entry point for tooling.
- Implement internal `generateJSONSchema` that bootstraps metadata via `extractStructCache`, honoring aliases, `RegisterTagNameFunc`, and custom tag overrides.
- Resolve property names from `json` tags (strip `,omitempty`, skip `json:"-"`), then `RegisterTagNameFunc`, then struct field name.
- Map Go kinds to JSON Schema types (`string`, `integer`, `number`, `boolean`, `array` + `items`, `object` + `additionalProperties`, nested struct `properties`).
- Map `time.Time` to `string` with `format: date-time`.
- Map common `validate` constraints: `required`, `min`/`max`, `len`, `gt`/`gte`/`lt`/`lte`, `oneof` → `enum`, format tags (`email`, `url`, `uri`, `uuid`, `ipv4`, `ipv6`), built-in regex tags via `regexes.go`, and custom `regexp` patterns.
- Skip cross-field rules (`eqfield`, `required_with`, etc.) and structural tags (`-`, `structonly`, `omitempty`, `omitnil`, `omitzero`).
- Follow `dive` for slice/array element schemas and map value schemas.
- Add unit tests covering config structs, `omitempty`, `-` / `json:"-"`, `oneof`, nested structs, `dive` on slices, `RegisterTagNameFunc`, non-struct input errors, and marshal round-trip.

## Test plan

```text
go test -run TestJSONSchema -v
```

All 10 tests pass:

- `TestJSONSchema_simpleConfig` — required fields, string `minLength`/`maxLength`, integer `minimum`/`maximum`
- `TestJSONSchema_omitemptyNotRequired` — `omitempty` excludes field from `required`
- `TestJSONSchema_skipDashTag` — `validate:"-"` omits property
- `TestJSONSchema_jsonDashOmitted` — `json:"-"` omits property
- `TestJSONSchema_oneofEnum` — `oneof` → `enum`
- `TestJSONSchema_nestedStruct` — nested object with per-field `required` and bounds
- `TestJSONSchema_diveSlice` — `dive` applies constraints on `items`
- `TestJSONSchema_registerTagNameFunc` — custom tag name func overrides `json` names
- `TestJSONSchema_nonStructInput` — returns `InvalidValidationError` for non-struct input
- `TestJSONSchema_roundTripMarshal` — output includes `$schema` draft URL

## Closes

https://github.com/go-playground/validator/issues/1518