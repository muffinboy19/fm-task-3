## Root cause

`isHostnameRFC1123` only checks `hostnameRegexRFC1123()`, which allows labels to start with digits and treats each dot-separated segment as a hostname label. Strings like `277.168.0.1` match because `277`, `168`, `0`, and `1` are valid numeric labels under that pattern.

There is no follow-up for IPv4-shaped input. Per RFC 1123, dotted-decimal strings that look like IPv4 must be syntactically valid IPv4 before acceptance (see also issue #1327 in this repo). `net.ParseIP("277.168.0.1")` returns `nil`, but the validator never calls it, so invalid octets still pass.

The same gap exists in `isHostnamePort`, which reuses `hostnameRegexRFC1123().MatchString(host)` without an octet check.

## Proposed fix

1. **Add a narrow pre-check** in `baked_in.go` (unexported helper, e.g. `isHostnameRFC1123String(s string) bool`) used by both `isHostnameRFC1123` and `isHostnamePort`:
   - If `hostnameRegexRFC1123()` does not match → return `false`.
   - If the value is **four-part dotted decimal** (exactly four `.`-separated labels, each non-empty and all ASCII digits) → require `net.ParseIP(s) != nil && ip.To4() != nil`; otherwise return `false`.
   - Otherwise → return `true` (regex match is sufficient).

2. **Do not change** `hostnameRegexStringRFC1123` in `regexes.go` (maintainer feedback on PR #1562: regex-only fixes are insufficient; broad “digits and dots” checks are error-prone).

3. **Performance:** call `net.ParseIP` only when the four-part all-decimal shape is detected, not for every hostname (e.g. `example.com`, `1.foo.com`).

4. **Keep** `{"192.168.0.1", true}` in existing tests — RFC 1123 allows dotted-decimal host identity when it is valid IPv4; `fqdn` already rejects pure numeric forms via its TLD rule.

## Files to change

| File | Change |
|------|--------|
| `baked_in.go` | Extend `isHostnameRFC1123`; add small helper(s) for four-part decimal detection + shared RFC1123 hostname validation; update `isHostnamePort` host check to use the same logic |
| `validator_test.go` | Extend `TestHostnameRFC1123Validation` and `TestHostnameRFC1123AliasValidation`; optionally extend `Test_hostnameport_validator` for invalid IPv4 host |

No changes to `regexes.go`, translations, or `doc.go` unless you want doc wording about the dotted-decimal rule (optional, out of minimal scope).

## Test strategy

All tests stay in `validator_test.go`, same table-driven style and assertions (`IsEqual`, `getError`, tag `hostname_rfc1123` / `hostname`).

### `TestHostnameRFC1123Validation`

Add rows to the existing table (fail before fix, pass after):

| `param` | `expected` | Rationale |
|---------|------------|-----------|
| `277.168.0.1` | `false` | Issue repro: invalid first octet |
| `256.1.1.1` | `false` | Octet > 255 |
| `123.456.789.0` | `false` | Multiple invalid octets |
| `01.2.3.4` | `false` | Leading-zero label; `net.ParseIP` rejects |

Keep existing pass cases unchanged, especially:

| `param` | `expected` |
|---------|------------|
| `192.168.0.1` | `true` |
| `1.2.3.4` | `true` (add if missing — valid IPv4, should remain valid) |
| `example.com`, `1.foo.com` | `true` |

**Before fix:** run `go test -run TestHostnameRFC1123Validation`; new `277.168.0.1` row should fail with `Error: <nil>`.  
**After fix:** full `go test ./...` (or at least package root tests).

### `TestHostnameRFC1123AliasValidation`

Mirror the same new invalid IPv4 rows; keep `192.168.0.1` → `true`; assert tag `hostname` on failures.

### `Test_hostnameport_validator` (recommended)

Add:

| `data` | `expected` |
|--------|------------|
| `277.168.0.1:8080` | `false` |
| `192.168.1.1:1234` | `true` (existing) |

Ensures `isHostnamePort` stays consistent with `hostname_rfc1123`.

## Risks / assumptions

- **Assumption:** Fix scope is **invalid four-part dotted decimal** (reject when `net.ParseIP` fails), not banning all dotted-decimal hostnames. That matches existing `192.168.0.1` → `true`, RFC 1123 §2 host-identity guidance (#1327), and maintainer PR #1562 direction (four-part check only).
- **Behavior change:** Values like `01.2.3.4` that match the regex today will start failing; document if that is undesirable.
- **`isOrigin` (or similar) in `baked_in.go` ~1603** uses `net.ParseIP(hostname) == nil && hostnameRegexRFC1123()` and may still accept `277.168.0.1` via regex alone; out of scope for tag `hostname_rfc1123` unless explicitly requested.
- **No regex change** avoids breaking numeric multi-label names (`1.foo.com`, `24.example24.com`).
- Reuse `strings` + `net.ParseIP` only; no new dependencies.