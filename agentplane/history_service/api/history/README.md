# Retained History read contract

[`history_service.proto`](history_service.proto) defines the authenticated read API. Consumers
read runner-authored `EventEntry` values; physical PostgreSQL rows are not the interface.

History can losslessly compact native frames and streamed deltas while an item is still active.
The writer stores bounded gzip-compressed protobuf blocks and a cursor/kind/size index, then
reconstructs entries at the read boundary. This is internal storage, not a new event type.

Correctness requirements:

- Packing preserves each entry's cursor, origin, timestamp, source references, protobuf values
  and unknown fields. A native frame's `line` string stays byte-for-byte equal. An interrupted,
  failed or never-completed item needs no future completion to remain readable.
- `ReadSessionEvents` and `FollowSession` return the same ordered entries and watermarks whether
  the requested cursor lies inside a block, in an unpacked tail, or between storage forms.
  Kind filtering and `StartBack` count logical entries. Follow's event/byte limits apply to the
  reconstructed response; an oversized entry is still returned alone.
- Observation pagination and native/evidence lookups retain their original cursor identities.
  The fold consumes exactly the same events. Batched sequence reads decompress a selected block
  once and do not issue one database query per requested source sequence.
- An ingress retry at a packed cursor is checked against the original event. Equal retries are
  idempotent; changed events conflict. Packing does not turn these cursors into settled gaps.
- Packing, singleton deletion and the ingress acknowledgement watermark commit atomically under
  the Session writer lock. Readers use one snapshot for indexes, bodies, watermarks and existing
  settlement/removal records. Corrupt or missing block content fails the read.
- Existing completion-based settlement remains independent and may remove proven redundant
  pairs from either storage form. Only settlement/removal records authorize missing cursors.

`ingress.compact_deltas` defaults to `false` and controls new writes only. Readers always support
both representations. Enabling starts with the next appended prefix; it does not synchronously
rewrite old Sessions. Turning it off leaves packed history readable and sends new writes to the
ordinary representation. Unpacking is required before reverting to a binary without block readers.

Blocks contain at most 256 entries and approximately 128 KiB of uncompressed event bodies. Larger
individual frames remain ordinary entries. A block is never extended with an accumulated prefix;
only explicit settlement can prune an existing block. This bounds decompression and rewrite work.
Only eligible entries written with the flag enabled enter the packer's partial index. Old history
and oversized frames cannot cause repeated scans of a growing unpacked prefix.
