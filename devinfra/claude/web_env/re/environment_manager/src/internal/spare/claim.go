// Reconstructed from binary: environment-manager (Build ID 0b86a2a0, version
// release-1186d93b9-ext). NEW in this build.
//
// The claim side of the warm-spare mechanism: how a task-run takes over a
// pre-booted Claude Code process, and how a failure to do so is attributed to a
// reason for the claude_code.spare.claim_miss counter.

package spare

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"syscall"
	"time"
)

// Claim-miss reason values for the claude_code.spare.claim_miss counter
// ("Cold spawn instead of warm-spare claim, attributed by reason").
//
// The counter is emitted by FKPKJ5B0zZ.BzcenmeCMsXD(ctx, reason string)
// (0x200c0c0), which builds a one-entry attribute map and forwards to the o11y
// increment helper. It has exactly four call sites, all inside
// TaVHwGAw.(*EAT9SthH).Execute:
//
//	0x2167f90 -> ReasonSkippedDockerWrapper (literal, via outlined block 0x2222da0)
//	0x2168803 -> ReasonPreloadDisabled / ReasonSpawnFailed (two branches on the
//	             same materialisation, selected at 0x2167fcf)
//	0x2172ae1 -> TODO(re): unrecovered, see below
//	0x2172c98 -> the dynamic value returned by (*Spare).ClaimFailReason
//
// Every literal below was read out of a live process after force-executing its
// decryption block; they are ground truth.
const (
	// Binary: 0x2167f90 emit site; literal from the outlined block at 0x2222da0.
	// Emitted before any spare is considered, on the path where the Docker
	// wrapper is skipped — the spare and the wrapper are mutually exclusive.
	ReasonSkippedDockerWrapper = "skipped_docker_wrapper"

	// Binary: 0x2168803 emit site, branch taken when the guard at 0x2167fcc
	// (TESTQ BX,BX / JNE 0x2168026) is false.
	ReasonPreloadDisabled = "preload_disabled"

	// Binary: 0x2168803 emit site, the 0x2168026 branch — the spare was wanted
	// but Spawn failed.
	ReasonSpawnFailed = "spawn_failed"

	// The four values below are returned by (*Spare).ClaimFailReason
	// (0x21ad700) and forwarded to the counter at 0x2172c98.

	// Binary: 0x21ad74a — process not alive AND the early-exit field (+0x74) is
	// non-zero: the spare died before it could ever have served a claim.
	ReasonSpareDiedEarly = "spare_died_early"

	// Binary: 0x21ad776 — process not alive, early-exit field zero.
	ReasonSpareDied = "spare_died"

	// Binary: 0x21ad7c8 — process alive, but the dial failure was not a
	// "socket not up yet" error, so something else went wrong.
	ReasonClaimError = "claim_error"

	// Binary: outlined block 0x2227e80 (ClaimFailReason.func3), tail-called at
	// 0x21ad873 — process alive and the dial kept failing with ENOENT or
	// ECONNREFUSED until the deadline: the spare never bound the socket in time.
	ReasonSocketTimeout = "socket_timeout"
)

// TODO(re): a fifth literal claim-miss reason is emitted at 0x2172ae1. Its
// buffer (38 bytes; the bounds checks at 0x2172422-0x21724da give the length)
// is produced by the shared literal state machine at 0x21724df, whose entry
// block could not be isolated, so forced execution could not decrypt it. It
// sits on the branch immediately preceding the ClaimFailReason attribution, so
// it is the "we had a spare but never got as far as dialling" case.

// Claim connects to the spare's socket and takes ownership of the pre-booted
// process.
//
// Binary: 0x21ab7c0 - TaVHwGAw.(*Qx7xZhVlaq46).Claim
//
// Call structure, in order:
//
//	0x21ab839 TaVHwGAw.eMUiM9ihRU        env []string -> map (see below)
//	0x21ab8b4 context.With*              (Ciyypbbc_.NFWAFIyls)
//	0x21ab959 time.Now / 0x21ab968 Time.Add   -> deadline
//	0x21ab9a8 Claim.func1                -> "claude-preload claim marshal: %w"
//	0x21aba23 time.Sleep                 (retry backoff)
//	0x21aba57 net.Dial                   (x3ZgH1.E7tk69QN)
//	0x21aba79 time.Now / 0x21aba96 Time.After  -> deadline expired?
//	0x21abaca TaVHwGAw.j7U_wOYPy         -> retryable dial error?
//	0x21abb08 Claim.func3                -> "claude-preload claim connect: %w"
//	0x21abd71 MOVB $0xa                  append '\n' to the payload
//	0x21abd86 conn.Write                 (itab slot +0x50)
//	0x21abe6d MOVB $0x1,0x71(recv)       mark claimed
//	0x21abfd2 literal                    -> "claude-preload claimed"
//
// So the wire protocol is a single newline-terminated JSON line written to the
// Unix socket — the "marshal" error format proves the payload is marshalled,
// and the '\n' is appended to the marshalled bytes immediately before Write.
// One of the payload's field names is recovered: "session_id" (Claim.func7 at
// 0x2229260, materialised at 0x21ac989; the same literal appears again in the
// Execute claim path at 0x2172d69).
//
// TODO(re): the full payload struct is not recovered. Its field names are not
// in the binary's JSON struct-tag table, so either the struct uses default
// (unnamed) tags or the payload is assembled as a map.
//
// TODO(re): whether Claim reads a reply back off the connection is not
// determined — only the two Write error paths were recovered.
func (s *Spare) Claim(ctx context.Context, sessionID string, env []string, timeout time.Duration) error {
	// Binary: 0x21ab839 - eMUiM9ihRU(env) builds a map[string]string by
	// splitting each entry at "=" (0x21ae6a0: makemap +
	// internal/stringslite.Cut + mapassign_faststr). Its single recovered
	// literal is "_FILE_DESCRIPTOR" (0x21ae745), compared with runtime.memequal.
	// TODO(re): the full env key is not recovered — only the 16-character
	// suffix "_FILE_DESCRIPTOR". This is the one place the claim path looks at
	// an inherited file descriptor, so it is the likely fd-passing hook.
	envMap := parseEnv(env)

	deadline := time.Now().Add(timeout)

	payload, err := json.Marshal(claimPayload{SessionID: sessionID, Env: envMap})
	if err != nil {
		// Binary: Claim.func1 (0x22281e0), materialised at 0x21ab9ad.
		return fmt.Errorf("claude-preload claim marshal: %w", err)
	}

	var conn net.Conn
	for {
		// Binary: 0x21aba57 - net.Dial over the spare's socket.
		conn, err = net.Dial("unix", s.sock)
		if err == nil {
			break
		}
		// Binary: 0x21abaca - j7U_wOYPy(err) reports whether the socket simply
		// is not up yet, so the dial is worth retrying.
		if !retryableDialError(err) || time.Now().After(deadline) {
			// Binary: Claim.func3 (0x2228640), materialised at 0x21abb0d.
			return fmt.Errorf("claude-preload claim connect: %w", err)
		}
		// Binary: 0x21aba23 - time.Sleep between attempts.
		// TODO(re): the retry interval constant is not recovered.
		time.Sleep(claimRetryInterval)
	}
	defer conn.Close()

	// Binary: 0x21abd71 appends '\n', 0x21abd86 calls conn.Write.
	if _, err := conn.Write(append(payload, '\n')); err != nil {
		// Binary: literals materialised at 0x21abd89/0x21abe65 — the same
		// format string on both write paths.
		return fmt.Errorf("claude-preload claim write: %w", err)
	}

	// Binary: 0x21abe6d - MOVB $0x1, 0x71(receiver).
	s.claimed = true
	// Binary: literal at 0x21abfd2, materialised at 0x21ac951.
	s.log.Info("claude-preload claimed")
	return nil
}

// ClaimFailReason attributes a failed claim to one of the claim_miss reasons.
//
// Binary: 0x21ad700 - TaVHwGAw.(*Qx7xZhVlaq46).ClaimFailReason
//
// Control flow read directly off the disassembly:
//
//	0x21ad732  call pL1QBZl_VraK (alive)
//	0x21ad737  TESTB AL,AL / JNE 0x21ad7a5      -> alive: skip to the dial check
//	0x21ad743  MOVL 0x74(recv),EDX / TESTL      -> early-exit field
//	0x21ad74a  ...                              -> "spare_died_early"
//	0x21ad776  ...                              -> "spare_died"
//	0x21ad7b5  call j7U_wOYPy(err)
//	0x21ad7c2  TESTB AL,AL / JNE 0x21ad859      -> retryable: socket never came up
//	0x21ad7c8  ...                              -> "claim_error"
//	0x21ad873  tail-call ClaimFailReason.func3  -> "socket_timeout"
func (s *Spare) ClaimFailReason(err error) string {
	if !s.alive() {
		if s.exitStatus != 0 {
			return ReasonSpareDiedEarly
		}
		return ReasonSpareDied
	}
	if !retryableDialError(err) {
		return ReasonClaimError
	}
	return ReasonSocketTimeout
}

// retryableDialError reports whether a dial failure means "the spare has not
// bound its socket yet" rather than a real failure.
//
// Binary: 0x21ad680 - TaVHwGAw.j7U_wOYPy. Two errors.Is calls
// (kQPv6Na0ka.LlDav2QfWZU) against sentinels sharing itab 0x2baa0a0
// (syscall.Errno) with data words 0x2ba1b30 = 0x02 (ENOENT) and
// 0x2ba60b0 = 0x6f = 111 (ECONNREFUSED).
func retryableDialError(err error) bool {
	return errors.Is(err, syscall.ENOENT) || errors.Is(err, syscall.ECONNREFUSED)
}

// claimRetryInterval is the sleep between dial attempts.
//
// TODO(re): value not recovered — the time.Sleep argument at 0x21aba23 comes
// from a register whose provenance was not traced.
const claimRetryInterval = 25 * time.Millisecond

// claimPayload is the JSON line written to the spare's socket.
//
// TODO(re): only the "session_id" field name is recovered (Claim.func7 at
// 0x2229260). The remaining fields, and whether the environment map is part of
// the payload at all, are unrecovered — the envMap is built at the top of Claim
// but its consumer was not traced.
type claimPayload struct {
	SessionID string            `json:"session_id"`
	Env       map[string]string `json:"-"` // TODO(re): tag unknown; may not be sent.
}

// parseEnv splits KEY=VALUE entries into a map.
//
// Binary: 0x21ae6a0 - TaVHwGAw.eMUiM9ihRU (makemap + stringslite.Cut +
// mapassign_faststr + memequal against "_FILE_DESCRIPTOR").
func parseEnv(env []string) map[string]string {
	// TODO(re): stub — the memequal against "_FILE_DESCRIPTOR" implies the
	// function also selects one specific variable, which is not reconstructed.
	m := make(map[string]string, len(env))
	for _, e := range env {
		if k, v, ok := cut(e, "="); ok {
			m[k] = v
		}
	}
	return m
}

// TODO(re): the binary calls internal/stringslite.Cut directly; this is the
// exported equivalent.
func cut(s, sep string) (before, after string, found bool) {
	for i := 0; i+len(sep) <= len(s); i++ {
		if s[i:i+len(sep)] == sep {
			return s[:i], s[i+len(sep):], true
		}
	}
	return s, "", false
}
