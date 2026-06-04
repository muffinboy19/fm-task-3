# Fix AsciiJSON non-BMP escaping with UTF-16 surrogate pairs

## Summary

`render.AsciiJSON` escaped every non-ASCII rune with `\u%04x`, but `%04x` only sets a minimum width. Supplementary-plane code points (e.g. emoji) produced 5+ hex digits in a single `\u` token, which is invalid per RFC 8259 and silently corrupts the decoded string while still producing syntactically valid JSON. This PR branches on BMP vs non-BMP runes: BMP characters keep the existing single `\uXXXX` escape, and supplementary characters are encoded as a UTF-16 surrogate pair via `unicode/utf16.EncodeRune`.

## Changes

- Import `unicode/utf16` in `render/json.go` and emit two `\u%04x` escapes for runes above U+FFFF
- Keep single `\u%04x` escaping for BMP runes (U+0080–U+FFFF); existing behavior for `GO语言`, `<br>`, etc. is unchanged
- Increase `escapeBuf` preallocation from 6 to 12 bytes to cover two escape sequences per rune
- Add `nonBMP` subtest to `TestRenderAsciiJSON` asserting wire format `{"msg":"\ud83d\ude00"}` and round-trip via `json.Unmarshal`
- Add `supplementaryBoundary` subtest for U+10000 (`\ud800\udc00`) with round-trip assertion

## Test plan

- [x] `go test ./render/...` — pass (`ok github.com/gin-gonic/gin/render 0.311s`)
- [x] `TestRenderAsciiJSON` — existing BMP cases unchanged; new `nonBMP` and `supplementaryBoundary` subtests cover emoji and first supplementary code point
- [x] `TestRenderAsciiJSONFail` — unchanged (marshal error path)

## Closes

https://github.com/gin-gonic/gin/issues/4688