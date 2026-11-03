# Stream analysis decoding

`analysis_codec::decode` inflated the complete compressed MessagePack body into
a `Vec<u8>`, then deserialized the analysis while retaining that buffer. Cached
pipeline and graph-query processes therefore held both the inflated bytes and
the reconstructed facts during decoding. The input artifact's compressed size
does not bound the inflated buffer.

The reader now feeds `rmp_serde` from a zstd decoder. Its existing
`DeserializeOwned` contract requires no values to borrow from the input, and
the context-ordinal adapter and shared-Arc scope remain active for the same
deserialization. Encoding and the artifact format are unchanged.

After deserializing the value, the reader drains the remaining compressed stream
to a sink. Stopping at the MessagePack value alone could skip a frame checksum
or later compressed data and accept corruption that full decompression rejects.
The drain retains validation without retaining the decompressed body.

This removes the full inflated buffer from the reader's live allocations. No
memory profile, peak-memory saving, corpus performance result, or fit under the
canonical resource budget is established by source inspection. Analyze-time
memory remains a separate concern. Validation uses the existing codec and
staged-analysis controls in normal CI; no tests were added or run locally.
