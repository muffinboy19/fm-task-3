# Link JSON Schema generation example in README

## Summary

Issue [#1518](https://github.com/go-playground/validator/issues/1518) requests JSON Schema generation from `validate` struct tags for config and documentation. Maintainers have indicated this should stay out of core and be addressed via an optional companion module (wrapping [invopop/jsonschema](https://github.com/invopop/jsonschema) with `validate`-tag mapping), not by exporting internal validator cache types.

This PR is documentation-only: it adds a README examples entry pointing to `_examples/jsonschema/main.go` so users can discover the intended JSON Schema workflow once that example and the `jsonschema/` module land. It does not add `Reflect`, tag mapping, tests, or the example itself.

## Changes

- Add a **JSON Schema generation** bullet under README **Examples**, linking to `_examples/jsonschema/main.go`.

## Test plan

- No Go code changed; `go test` is unchanged.
- Manually confirmed the patch is a single README examples link (no `jsonschema/` module, API, mapping, tests, `doc.go` cross-link, or runnable example in this diff).
- Follow-up: after the `jsonschema/` module and `_examples/jsonschema/main.go` exist, re-check the link and run `go test ./...` under `jsonschema/` and the example.

## Closes

https://github.com/go-playground/validator/issues/1518

(Relates to / does not fully implement the feature in this diff—implementation remains follow-up work.)