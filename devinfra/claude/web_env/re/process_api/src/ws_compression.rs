//! Reverse-engineered from process_api BuildID edebff2c28de76238c95c299ba3401a9098c9e17
//! release process_api_2026-05-11-18-55
//!
//! WebSocket payload compression (zstd). New module in edebff2c — the source
//! path `src/ws_compression.rs` appears in the binary's panic-location table
//! (`.data.rel.ro` 0x4216a8..0x421738, six `core::panic::Location` records).
//!
//! Panic locations recovered from the binary (file string 0x389a3d, len 21):
//!
//! | Location addr | Line | Col | Message                                                     |
//! | ------------- | ---- | --- | ----------------------------------------------------------- |
//! | `0x4216f0`    | 69   | 14  | `zstd encoder init into Vec<u8> is infallible`               |
//! | `0x421708`    | 71   | 14  | `ZSTD_c_windowLog with static in-range param is infallible`  |
//! | `0x421720`    | 87   | 14  | `zstd stream encode into Vec<u8> is infallible`              |
//! | `0x4216a8`    | 104  | 14  | `zstd decoder init over BoundedSink is infallible`           |
//! | `0x4216c0`    | 106  | 14  | `ZSTD_d_windowLogMax with static in-range param is infallible` |
//! | `0x4216d8`    | 120  | 14  | (context location for `zstd stream decode`)                 |
//!
//! Other string refs in this module:
//!   "encoder is finished"                          (0x3995f8)
//!   "writer will not accept any more data"         (0x399311)
//!   "decompressed output exceeds {} bytes"         (template 0x389c9e)
//!   "zstd stream decode"                           (0x3993a1)
//!
//! Linked against `zstd-safe 7.2.4` (panic path
//! `/root/.cargo/registry/src/artifactory.infra.ant.dev-.../zstd-safe-7.2.4/src/lib.rs`)
//! plus the bundled `zstd` C library (its full error-string table is at
//! 0x3abe08..0x3ac360, e.g. "Frame requires too much memory for decoding").
//!
//! Wire negotiation (see `io.rs`): the client advertises `accept_zstd` in the
//! `ProcessConnection` first message; the server answers with
//! `ConnectionCapabilities { supports_trace, supports_zstd }`.

/// `ZSTD_c_compressionLevel` (parameter id 100) value.
/// Decompiled from 0x11ff40..0x11ff4d: `mov $0x64,%esi; mov $0x3,%edx`.
const ZSTD_COMPRESSION_LEVEL: i32 = 3;

/// `ZSTD_c_windowLog` (parameter id 101) value — 32 KiB compression window.
/// Decompiled from 0x11ffa2..0x11ffaf: `mov $0x65,%esi; mov $0xf,%edx`.
const ZSTD_WINDOW_LOG: u32 = 15;

/// `ZSTD_d_windowLogMax` (parameter id 100 on the DCtx) — must match the
/// encoder's window so a hostile peer cannot force a large decoder allocation.
/// Decompiled from 0x14bf5b..0x14bf68: `mov $0x64,%esi; mov $0xf,%edx`.
const ZSTD_DECODE_WINDOW_LOG_MAX: u32 = 15;

/// Scratch buffer allocated for both the encoder and the decoder.
/// Decompiled from 0x11ff8b (`mov $0x8000,%edi; call __rust_alloc`) and
/// 0x14bf44 (same shape on the decode side).
const ZSTD_BUF_SIZE: usize = 0x8000;

/// Hard cap on the number of bytes a single decompression may produce.
/// Decompiled from 0x14bfc2: `movq $0x4000000, 0xf8(%r12)` — the `limit`
/// field of the bounded sink, 64 MiB.
const MAX_DECOMPRESSED_BYTES: u64 = 0x0400_0000;

/// Streaming zstd encoder used for one direction of one WebSocket connection.
///
/// Constructed once per output stream by `io::handle_ws` (call sites 0x147d45
/// for stdout and 0x1487a6 for stderr, both dispatching on the per-connection
/// `zstd enabled` bool at `0x90(%rsp)`).
///
/// Decompiled from 0x11ff20..0x120200 (constructor) and 0x1bd5c0..0x1bf330
/// (`encode`). Struct layout read from the constructor's stores at
/// 0x11ffc9..0x120000:
///
/// ```text
/// +0x00  u64   0                 (bytes produced so far)
/// +0x08  ptr   ZSTD_CStream*
/// +0x10  u64   0                 (input cursor)
/// +0x18  u64   1
/// +0x20  u64   0                 (output cursor)
/// +0x28  u64   0x8000            (output buffer capacity)
/// +0x30  ptr   output buffer
/// +0x38  [u8;16] 0
/// +0x48  u16   0                 (finished / error flags)
/// ```
pub struct StreamEncoder {
    /// `zstd_safe::CCtx` handle. Created at 0x11ff2e via the `ZSTD_createCStream`
    /// GOT slot 0x42bf68.
    // TODO(re): concrete type not recovered — the binary only shows the raw
    // `ZSTD_CStream*`; whether the Rust side holds `zstd::stream::raw::Encoder`
    // or a bare `zstd_safe::CCtx` was not determined.
    cctx: ZstdCCtx,
    /// 32 KiB staging buffer for compressed output (0x11ff8b).
    buf: Vec<u8>,
    /// Set once `finish()` has been called; further `encode()` calls panic with
    /// "encoder is finished" (0x1be74c).
    finished: bool,
}

// STUB: opaque stand-in for the C `ZSTD_CStream*` the binary stores at +0x08.
// The real code links `zstd-safe 7.2.4`; this placeholder keeps the recovered
// control flow readable without inventing an API surface.
pub struct ZstdCCtx;

impl StreamEncoder {
    /// Decompiled from 0x11ff20..0x120200.
    ///
    /// Xrefs: "zstd encoder init into Vec<u8> is infallible" (0x3993b3, panic
    /// at line 69 via Location 0x4216f0), "ZSTD_c_windowLog with static
    /// in-range param is infallible" (0x3993df, line 71 via Location 0x421708).
    pub fn new() -> Self {
        // 0x11ff2e: ZSTD_createCStream(); NULL -> panic (0x120013).
        let cctx = zstd_create_cstream();

        // 0x11ff40..0x11ff61: ZSTD_CCtx_setParameter(cctx, 100 /* level */, 3),
        // then ZSTD_isError on the result.
        // 0x11ff67..0x11ff85: second call through GOT slot 0x42bf58 with
        // (cctx, 1, 0) — session reset — also checked with ZSTD_isError.
        // Both failure paths converge on the line-69 panic.
        // TODO(re): the (cctx, 1, 0) call target was not resolved to a named
        // zstd entry point; only its argument shape was read.
        zstd_set_c_parameter(
            &cctx,
            ZSTD_C_COMPRESSION_LEVEL,
            ZSTD_COMPRESSION_LEVEL as u32,
        )
        .expect("zstd encoder init into Vec<u8> is infallible");

        // 0x11ff8b: alloc(0x8000, align 1) for the output staging buffer.
        let buf = Vec::with_capacity(ZSTD_BUF_SIZE);

        // 0x11ffa2..0x11ffc3: ZSTD_CCtx_setParameter(cctx, 101 /* windowLog */, 15).
        zstd_set_c_parameter(&cctx, ZSTD_C_WINDOW_LOG, ZSTD_WINDOW_LOG)
            .expect("ZSTD_c_windowLog with static in-range param is infallible");

        Self {
            cctx,
            buf,
            finished: false,
        }
    }

    /// Compress one chunk of child stdout/stderr into the stream.
    ///
    /// Decompiled from 0x1bd5c0..0x1bf330. Called from the stderr pipe pump
    /// at 0x4a6ed / 0x4ae45 (fn 0x4a1f0, xref "[DEBUG] started stderr pipe")
    /// and the stdout pipe pump at 0x4c80d / 0x4cf65 (fn 0x4c310).
    ///
    /// Xrefs: "encoder is finished" (0x3995f8, 0x1be74c),
    ///   "zstd stream encode into Vec<u8> is infallible"
    ///   (0x399418, panic at line 87 via Location 0x421720).
    pub fn encode(&mut self, chunk: &[u8]) -> Vec<u8> {
        assert!(!self.finished, "encoder is finished");

        // TODO(re): the ZSTD_compressStream2 loop body (0x1bd5c0..0x1bf1bf) was
        // not traced instruction by instruction; only the panic edges and the
        // "encoder is finished" precondition were recovered.
        let _ = chunk;
        zstd_compress_stream(&self.cctx, chunk, &mut self.buf)
            .expect("zstd stream encode into Vec<u8> is infallible");
        std::mem::take(&mut self.buf)
    }
}

/// Output sink that refuses to accept more than `MAX_DECOMPRESSED_BYTES`.
///
/// Decompiled from the initialization at 0x14bf82..0x14bff9 (fields written
/// into the `process_ws_message` future's frame at `0xd0(%r12)`) and the
/// bound check at 0x14c510..0x14c57c.
///
/// Xrefs: "writer will not accept any more data" (0x399311, refs at 0x14c8a6
///   and 0x14ccfe), "decompressed output exceeds {} bytes" (template 0x389c9e,
///   refs at 0x14c56d and 0x14cb83).
pub struct BoundedSink {
    /// Bytes written so far — field at +0xf0.
    written: u64,
    /// Cap — field at +0xf8, initialized to 0x4000000 (0x14bfc2).
    limit: u64,
    out: Vec<u8>,
}

impl BoundedSink {
    /// Decompiled from 0x14c510..0x14c620.
    ///
    /// The binary compares `written + n` against `limit` (0x14c53b
    /// `cmpq 0xf8(%r12),%rax`) and on overflow formats the error with the
    /// limit — not the attempted size — as the single `{}` argument
    /// (0x14c549 loads `&limit` into the format argument slot).
    pub fn write(&mut self, data: &[u8]) -> Result<(), String> {
        if self.written + data.len() as u64 > self.limit {
            return Err(format!("decompressed output exceeds {} bytes", self.limit));
        }
        self.out.extend_from_slice(data);
        self.written += data.len() as u64;
        Ok(())
    }
}

/// Streaming zstd decoder for inbound (client -> server) binary frames.
///
/// Decompiled from 0x14bee3..0x14bff9, inlined into `io::process_ws_message`
/// (fn 0x14bc40). Creation is gated on the per-connection zstd flag at
/// `0xe8(%rsp)` (0x14bee3 `cmpb $0x0,0xe8(%rsp)`).
///
/// Xrefs: "zstd decoder init over BoundedSink is infallible" (0x399335, panic
///   at line 104 via Location 0x4216a8), "ZSTD_d_windowLogMax with static
///   in-range param is infallible" (0x399365, line 106 via Location 0x4216c0),
///   "zstd stream decode" (0x3993a1, error context at 0x14daf6 carrying
///   Location 0x4216d8 = line 120).
pub struct StreamDecoder {
    dctx: ZstdDCtx,
    buf: Vec<u8>,
    sink: BoundedSink,
}

// STUB: opaque stand-in for the C `ZSTD_DStream*` stored at +0xd8.
pub struct ZstdDCtx;

impl StreamDecoder {
    /// Decompiled from 0x14bee3..0x14bff9.
    pub fn new() -> Self {
        // 0x14bef1: ZSTD_createDStream() through GOT slot 0x42bbb0; NULL -> 0x14e336.
        let dctx = zstd_create_dstream();

        // 0x14bf06..0x14bf3e: two calls (GOT 0x42be18, then 0x42bfa0 with
        // (dctx, 1, 0)) each checked with ZSTD_isError; both failure edges land
        // on the line-104 panic at 0x14e4a3.
        zstd_reset_dctx(&dctx).expect("zstd decoder init over BoundedSink is infallible");

        // 0x14bf44: alloc(0x8000) input/output scratch.
        let buf = Vec::with_capacity(ZSTD_BUF_SIZE);

        // 0x14bf5b..0x14bf7c: ZSTD_DCtx_setParameter(dctx, 100 /* d_windowLogMax */, 15).
        zstd_set_d_parameter(&dctx, ZSTD_D_WINDOW_LOG_MAX, ZSTD_DECODE_WINDOW_LOG_MAX)
            .expect("ZSTD_d_windowLogMax with static in-range param is infallible");

        Self {
            dctx,
            buf,
            sink: BoundedSink {
                written: 0,
                limit: MAX_DECOMPRESSED_BYTES,
                out: Vec::new(),
            },
        }
    }

    /// Decompiled from the decode arm of fn 0x14bc40 (0x14c460..0x14dbb0).
    ///
    /// On a zstd library error the frame is wrapped with the context string
    /// "zstd stream decode" carrying this module's line-120 `Location`.
    pub fn decode(&mut self, frame: &[u8]) -> Result<Vec<u8>, String> {
        // TODO(re): the ZSTD_decompressStream pump is inlined into the async
        // state machine of `process_ws_message` and was not traced statement by
        // statement; the bound check, the two error strings and the parameter
        // setup above are what the disassembly establishes.
        let _ = frame;
        Err("zstd stream decode".to_string())
    }
}

// ---------------------------------------------------------------------------
// zstd-safe shims
//
// STUB: the real binary calls into the statically linked zstd C library through
// GOT slots (0x42bf68 createCStream, 0x42ba00 CCtx_setParameter, 0x42ba58
// isError, 0x42bbb0 createDStream, 0x42bd98 DCtx_setParameter). These
// declarations exist only so the recovered control flow above type-checks.
// ---------------------------------------------------------------------------

const ZSTD_C_COMPRESSION_LEVEL: u32 = 100;
const ZSTD_C_WINDOW_LOG: u32 = 101;
const ZSTD_D_WINDOW_LOG_MAX: u32 = 100;

fn zstd_create_cstream() -> ZstdCCtx {
    ZstdCCtx
}

fn zstd_create_dstream() -> ZstdDCtx {
    ZstdDCtx
}

fn zstd_set_c_parameter(_ctx: &ZstdCCtx, _param: u32, _value: u32) -> Result<(), &'static str> {
    Ok(())
}

fn zstd_set_d_parameter(_ctx: &ZstdDCtx, _param: u32, _value: u32) -> Result<(), &'static str> {
    Ok(())
}

fn zstd_reset_dctx(_ctx: &ZstdDCtx) -> Result<(), &'static str> {
    Ok(())
}

fn zstd_compress_stream(
    _ctx: &ZstdCCtx,
    _input: &[u8],
    _out: &mut [u8],
) -> Result<(), &'static str> {
    Ok(())
}
