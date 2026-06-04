## Root cause

`AsciiJSON.Render` in `render/json.go` escapes every rune above `unicode.MaxASCII` with `fmt.Appendf(..., "\\u%04x", r)`. For `%04x`, width 4 is a **minimum**, so supplementary-plane code points (U+10000 and above) produce **5+** hex digits (e.g. U+1F600 → `\u1f600`). RFC 8259 requires **exactly four** hex digits per `\u` escape; decoders treat the first four as one code unit and any extra digits as literal text. The JSON stays valid but the string no longer round-trips.

## Proposed fix

In the `r > unicode.MaxASCII` branch of `AsciiJSON.Render`:

1. **BMP** (`r <= 0xFFFF`): keep the current single `\u%04x` escape (unchanged behavior for existing tests like `GO语言` and `<br>`).
2. **Non-BMP** (`r > 0xFFFF`): encode per RFC 8259 using a UTF-16 surrogate pair via `unicode/utf16.EncodeRune(r)`, then write **two** `\u%04x` escapes (e.g. U+1F600 → `\ud83d\ude00`).

Reuse the existing `escapeBuf` + `fmt.Appendf` pattern; for supplementary runes, either append twice with `escapeBuf[:0]` or one `Appendf` with two `%04x` verbs. Bump `escapeBuf` capacity from 6 to **12** (two `\uXXXX` sequences) and update the comment.

No changes to `Context.AsciiJSON`, content-type handling, or marshaling — only the post-marshal ASCII-escaping loop.

## Files to change

| File | Change |
|------|--------|
| `render/json.go` | Import `unicode/utf16`; branch on `r > 0xFFFF` to emit surrogate pair; adjust `escapeBuf` cap/comment |
| `render/render_test.go` | Add non-BMP coverage to `TestRenderAsciiJSON` (or a `t.Run` subtest) |

## Test strategy

All tests in `render/render_test.go`, same package and testify style as siblings.

1. **Extend `TestRenderAsciiJSON`** (recommended: `t.Run("nonBMP", ...)`)  
   - **Setup:** `httptest.NewRecorder()`, `AsciiJSON{Data: map[string]string{"msg": "😀"}}`  
   - **Render:** `require.NoError(t, err)`  
   - **Wire format:** `assert.Equal(t, `{"msg":"\ud83d\ude00"}`, w.Body.String())` — exact lowercase hex, surrogate pair, **not** `\u1f600`  
   - **Round-trip:** `json.Unmarshal` body into `map[string]string`; `assert.Equal(t, "😀", decoded["msg"])`  
   - **Failing-before / passing-after:** on current master, body is `{"msg":"\u1f600"}` and decoded value is not `"😀"`; both assertions fail before the fix and pass after.

2. **Regression guard for existing BMP behavior**  
   - Leave the existing `data1` / `data2` cases in `TestRenderAsciiJSON` unchanged; they must still pass (`GO\u8bed\u8a00`, `\u003cbr\u003e`, scalar `3.1415926`).

3. **Optional boundary subtest** `t.Run("supplementaryBoundary", ...)`  
   - Input: `"a\U00010000b"` (first supplementary code point)  
   - **Expected body fragment:** `\ud800\udc00` between `a` and `b`  
   - **Round-trip:** decoded string equals original  

4. **`TestRenderAsciiJSONFail`** — no change (marshal error path untouched).

Run: `go test ./render/... -run TestRenderAsciiJSON -v` (and full `go test ./...` if desired).

## Risks / assumptions

- **Assumption:** Lowercase `\u` hex is required to match existing tests and `%04x` output.
- **Assumption:** `json.API.Marshal` emits UTF-8 runes for string values (not pre-escaped `\u` sequences); the loop only needs to fix runes it encounters after marshal — same model as today.
- **Scope:** Lone UTF-16 surrogate code units (invalid Unicode scalars) are out of scope; behavior for those runes is unchanged.
- **Risk:** Minimal — only affects `AsciiJSON` output for supplementary characters; BMP and ASCII paths are identical.