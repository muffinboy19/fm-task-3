# Add opt-in JSON Schema generation from validate tags

## Summary

Issue [#1518](https://github.com/go-playground/validator/issues/1518) requests JSON Schema generation from struct tags so callers can document or validate complex configuration files alongside runtime validation. The core `validator` package has no introspection API for this today—tag parsing lives in unexported cache logic—and maintainers have indicated this should not ship in core.

This PR adds an opt-in `non-standard/jsonschema` subpackage (same pattern as `non-standard/validators`) that walks structs via reflection and maps common `validate` tags to JSON Schema constraints. It does not change core validation behavior.

## Changes

- Add `non-standard/jsonschema` with `Generate` and `GenerateSchema` entry points, plus functional options: `WithJSONTag` / `WithoutJSONTag`, `WithTagName`, `WithRequiredStructEnabled`, and `WithDraft` (default Draft 2020-12).
- Implement a read-only reflection walker that maps Go types to JSON Schema (`string`, `integer`, `number`, `boolean`, `object`, `array`), handles `dive` for nested structs/slices/maps, and sets `nullable` on optional pointer fields.
- Map common `validate` tags to schema keywords: `required`, `omitempty`/`omitnil`/`omitzero`, `min`/`max`/`len`/`eq`, `gt`/`gte`/`lt`/`lte`, `oneof` → `enum`, format tags (`email`, `url`, `uri`, `uuid`, `uuid4`, `ipv4`, `ipv6`), `unique` → `uniqueItems`, and `boolean` type override.
- Skip unexported fields and `validate:"-"`; use `json` tag names for property keys by default; support embedded struct field promotion.
- Ignore cross-field and unknown validation tags in v1 (e.g. `required_without`); map keys are not validated in schema (documented limitation).
- Add `_examples/jsonschema/main.go` demonstrating config schema generation.
- Link the example and package from `README.md`.

## Test plan

- [x] `go test ./non-standard/jsonschema/... -v` — all 12 tests pass:
  - Simple struct: `required`, `email` format, numeric `gte`/`lte`
  - JSON tag property naming (`e-mail`)
  - Skip `validate:"-"` fields
  - `omitempty` omitted from `required`
  - Nested struct, slice, and map `dive`
  - `oneof` → `enum`
  - Optional pointer → `nullable`
  - Cross-field tags ignored without error
  - Non-struct input returns error
  - `GenerateSchema` sets `$schema` draft URI

## Closes

https://github.com/go-playground/validator/issues/1518