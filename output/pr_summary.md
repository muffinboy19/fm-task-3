# Reject invalid IPv4-shaped hostnames in RFC1123 validation

## Summary

`hostname_rfc1123` (and the `hostname` alias) accepted dotted-decimal strings such as `277.168.0.1` because the RFC 1123 regex treats each segment as a valid numeric label, without checking whether the value is syntactically valid IPv4. Per RFC 1123, hostnames in dotted-decimal form are only allowed when they represent a valid IPv4 address. This PR adds a narrow pre-check: when input matches a four-part all-decimal pattern, it must pass `net.ParseIP` as IPv4; otherwise the existing regex match is sufficient. The same logic is applied in `isHostnamePort` so invalid IPv4-shaped hosts are rejected there too.

## Changes

- Add `isFourPartDottedDecimal` to detect exactly four non-empty, all-digit dot-separated labels.
- Add `isHostnameRFC1123String` to combine the RFC 1123 regex with an IPv4 octet check for four-part dotted-decimal input only.
- Update `isHostnameRFC1123` and `isHostnamePort` to use `isHostnameRFC1123String` instead of the regex alone.
- Extend `TestHostnameRFC1123Validation` and `TestHostnameRFC1123AliasValidation` with invalid cases (`277.168.0.1`, `256.1.1.1`, `123.456.789.0`, `01.2.3.4`) and valid `1.2.3.4`.
- Add `277.168.0.1:8080` as a failing case in `Test_hostnameport_validator`.
- No changes to `regexes.go` (regex-only fixes are insufficient per maintainer feedback).

## Test plan

- [x] `go test ./` — all tests pass (`ok github.com/go-playground/validator/v10 0.298s`)
- [x] `TestHostnameRFC1123Validation` — invalid IPv4-shaped hostnames fail; valid cases including `192.168.0.1` and `1.2.3.4` still pass
- [x] `TestHostnameRFC1123AliasValidation` — same coverage via `hostname` alias
- [x] `Test_hostnameport_validator` — `277.168.0.1:8080` rejected

## Closes

https://github.com/go-playground/validator/issues/1561