# go_crypto_server — reference spec

> Ground truth for the eval. **The agent under test does not see this file.**
> The judge uses it (and the rubric below) to grade the agent's recovered
> source against what the binary actually does.

A small HTTP service ("ncs" = note crypto service) that issues per-session
tokens, encrypts notes under a per-session key derived from the token, and
exports a MAC-signed snapshot of the session. Every cryptographic primitive is
deliberately weak and deliberately distinctive — the goal is for an RE'd
recovery to be either right or wrong by inspection, not paraphrasable.

## Wire protocol (`ncs/1`)

JSON over HTTP/1.1, all endpoints `POST`, all bodies envelope-shaped:

```json
{ "v": "ncs/1", "op": "<op-name>", "body": { ...op-specific... } }
```

Server responses use the same envelope. Errors bypass the envelope and use a
flat shape `{"v","op","code","reason"}` with HTTP 400 (client errors) or 500
(server errors).

| Path            | Op          | Request body                | Response body          |
| --------------- | ----------- | --------------------------- | ---------------------- |
| `/v1/register`  | `register`  | `{hint?: string}`           | `{token: hex32}`       |
| `/v1/note/put`  | `note.put`  | `{token, title, plaintext}` | `{note_id}`            |
| `/v1/note/get`  | `note.get`  | `{token, note_id}`          | `{title, plaintext}`   |
| `/v1/note/list` | `note.list` | `{token}`                   | `{note_ids: [string]}` |
| `/v1/export`    | `export`    | `{token}`                   | `{blob: string}`       |

`note_id` is `n_` + 16 uppercase hex digits (a process-wide counter,
zero-padded). Lists are sorted lexically.

Error codes: `4001` bad request, `4002` unknown token, `4003` unknown note,
`4004` bad ciphertext, `4005` bad MAC, `5001` internal, `5002` not implemented.

## Tokens & session key derivation

Token issuance uses a single shared splitmix64 PRNG seeded once at startup
with `time.Now().UnixNano() ^ os.Getpid()`. Each `register` consumes two
splitmix64 outputs (16 bytes total) and returns them as 32 lowercase hex.

**The session key is the token bytes.** `tokenToKey` does
`hex.DecodeString(token)` and uses the resulting 16 bytes verbatim as the
cipher key. There is no separate KDF — possessing the token is equivalent to
possessing the encryption key.

## Cipher (Feistel-8 / 64-bit block)

- Block size 8 bytes, key size 16 bytes, 8 rounds, BigEndian word order.
- Round function: `F(R, K) = sbox[(R ^ K) & 0xF] * 0x9E370001 ^ rotr(R, 7)`
  where the s-box is the 16-byte permutation:
  `{0x9, 0x4, 0xA, 0xB, 0xD, 0x1, 0x8, 0x5, 0x6, 0x2, 0x0, 0x3, 0xC, 0xE, 0xF, 0x7}`.
- Key schedule mixes the 4 32-bit words of the master key against round
  constants `{0xB7E15163, 0x9E3779B9, 0x243F6A88, 0x85A308D3, 0x13198A2E,
0x03707344, 0xA4093822, 0x299F31D0}`. Step `i`:
  `mixed = sum(state) + rc[i]; rk[i] = mixed ^ rotr(state[i&3], 3); state <<= 1; state[3] = mixed`.
- Mode: ECB with PKCS#7 padding (block size 8). Yes, ECB — distinctive
  fingerprint and another scoreable weakness.

## MAC

Hand-rolled Merkle–Damgård over the same Feistel cipher. Compression
function: `compress(state, block) = E_K(state XOR block)` where `E_K` reuses
the round keys derived from the same session key.

**Pre-image** is `key || len(key)/u32be || message`, then PKCS#7-padded to a
multiple of 8. **IV** is the constant
`{0xA5, 0x5A, 0x33, 0xCC, 0x96, 0x69, 0xF0, 0x0F}`. Tag is the final state's
first 8 bytes.

This MAC is **vulnerable to length extension** (because of the
`key || len || message` shape with no separation/finalization), and verifying
by `macSign + bytewise compare` is constant-time-ish but the construction is
broken regardless.

## Custom base32

32-char alphabet `"3456789ABCDEFGHJKLMNPQRSTUVWXYZ$"` — note skipped
`0/1/I/O/2`, no `0-9` prefix, `$` as the 32nd glyph. Padding char is `~`,
emitted to fill out 8-char groups. Encoder consumes 5 bytes at a time, packs
into 40 bits big-endian, slices into eight 5-bit indices.

## Export

`/v1/export` snapshots all notes for the session and returns
`base32(payload || mac_tag(payload))` where the payload is a flat
length-prefixed concatenation of `(len(note_id) u8, note_id, len(title) u8,
title, len(ct) u32be, ct)` per note in insertion order.

## Storage

In-memory, per-session: `sync.Mutex`-guarded map `token → session`, where
`session` holds the 16-byte key, an unordered map of `note_id → storedNote`,
and an order-preserving slice of `note_id`s. No persistence, no eviction.

## Endpoints, summarized

5 endpoints (above table), one mux. All other paths return 404 (stdlib mux
default).

## Concurrency

The `tokenSource` is `Mutex`-guarded; the `noteStore` is `Mutex`-guarded.
There is no rate limiting, no auth beyond token possession, no replay
protection, no nonce on encryption (ECB), no per-note IV.

## Out-of-scope, but worth noting for RE

- No TLS — bare HTTP.
- No persistence layer.
- The protocol version `ncs/1` is checked exactly; `wrong/0` is rejected with
  `4001`.

## Notes for the eval judge

The most important rubric items (cipher specifics, base32 alphabet, MAC
construction shape, key derivation) are **exact**. The agent's recovered
source either matches the constants verbatim or it doesn't. Paraphrased
descriptions ("uses a Feistel cipher with some rounds") should score zero.
