// Reconstructed from binary: environment-manager (Build ID 0b86a2a0, version
// release-1186d93b9-ext). NEW in this build.
//
// Source: internal/<claude-launcher>/spare.go (exact upstream package name not
// recovered — the garbled package is `TaVHwGAw`, 827 functions, which also
// contains the Claude Code executor).
//
// The warm-spare mechanism: `environment-runner preload-claude` pre-boots a
// Claude Code process with `--preload <socket>`; that process binds a Unix
// domain socket and idles. A later `task-run` claims the idle process over the
// socket instead of paying a cold start. The interval between the spare being
// spawned (or adopted) and the successful Claim is the "overlap window W"
// reported as claude_code.spare.spawn_to_claim_window_ms.
//
// Garble was built with -literals, so none of the strings below appear in
// `strings` output. Every literal quoted here was recovered by forced execution
// of its decryption block inside a live (breakpoint-frozen) process and is
// therefore ground truth, not inference.

package spare

import (
	"errors"
	"fmt"
	"log/slog"
	"os"
	"os/exec"
	"strings"
	"syscall"
	"time"
)

// SocketPath is the claim endpoint the spare binds and a claiming task-run
// dials. It is a hard-coded literal, not derived from $HOME: running
// preload-claude with HOME=/tmp/fakehome still produces this exact path.
//
// Binary: built at 0x24f39c7-0x24f3b28 inside qqGXzsqMa.EVAXAr5.func3 and read
// out of a live process as argv[2] of the exec (len 38).
const SocketPath = "/home/claude/.claude/remote/spare.sock"

// PreloadFlag is the flag handed to the claude binary to make it come up as a
// spare and listen on the socket rather than run a session.
//
// Binary: literal at 0x24f35ff (preload-claude), 0x21aa445 and 0x21aa9d3
// (Spawn), 0x21a8485 and 0x21a8765 (the /proc scanner), 0x21a9bd9 (the identity
// check). Verified live: execve argv is
// ["<claude>", "--preload", "/home/claude/.claude/remote/spare.sock"].
const PreloadFlag = "--preload"

// Spare is the handle a task-run holds on a pre-booted Claude Code process.
//
// Binary type: TaVHwGAw.Qx7xZhVlaq46. Field offsets confirmed from the method
// bodies; the struct is larger than modelled here.
//
//	+0x71 bool  — set to 1 at 0x21abe6d on a successful Claim ("claimed")
//	+0x74 int32 — non-zero selects "spare_died_early" over "spare_died"
//	              in ClaimFailReason (0x21ad743)
//
// TODO(re): the remaining fields (socket path, log file, cmd, stdout pipe) are
// not mapped to offsets; the names below are the recovered semantics, not
// recovered field offsets.
type Spare struct {
	// TODO(re): offsets unrecovered.
	cmd       *exec.Cmd
	pid       int
	sock      string
	spawnedAt time.Time
	adopted   bool

	claimed    bool  // +0x71
	exitStatus int32 // +0x74

	log *slog.Logger
}

// SpawnedAt reports when the spare process was started (or, for an adopted
// spare, when it was discovered).
//
// Binary: 0x21aa3c0 - TaVHwGAw.(*Qx7xZhVlaq46).SpawnedAt. It is a plain field
// load; the value is the origin of the spawn_to_claim window W.
func (s *Spare) SpawnedAt() time.Time { return s.spawnedAt }

// Adopted reports whether this spare was started by us or found already running.
//
// Binary: 0x21aa3e0 - TaVHwGAw.(*Qx7xZhVlaq46).Adopted (plain field load).
func (s *Spare) Adopted() bool { return s.adopted }

// Spawn starts a new Claude Code spare in the background.
//
// Binary: 0x21aa400 - TaVHwGAw.FvQ_rwQRpD
//
// Call structure (from disassembly):
//
//	syscall.Environ            0x21aa4xx (via syscall.HZehKkwsN)
//	TaVHwGAw.MuFihOu2FLD       env normaliser, shared with preload-claude
//	TaVHwGAw.DmttrbQxfow       GetClaudePath
//	os.OpenFile/Create + Write + Close   (daI_d2D7.TcBSNIODDy / (*os.File).*)
//	exec.Command               sokjaKw.GUaG4ewe
//	(*exec.Cmd).Start          0x115fc?? — Start, not Run: the spare outlives
//	                           this call and is reaped by Wait/Kill
//	time.Now / Time.Add / Time.After
//
// The only literal in the function body is PreloadFlag (0x21aa445, 0x21aa9d3),
// so the socket path and the log file path arrive as parameters.
//
// TODO(re): the log-file path, the Cmd.Env delta and the time.Add deadline
// (an internal spawn timeout) are not reconstructed.
func Spawn(log *slog.Logger, claudePath, sock, logPath string) (*Spare, error) {
	// TODO(re): stub — parameter order and the log-file wiring are inferred
	// from the call sequence, not read off the register ABI.
	f, err := os.OpenFile(logPath, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0o600)
	if err != nil {
		return nil, err
	}
	cmd := exec.Command(claudePath, PreloadFlag, sock)
	cmd.Env = NormalizeEnv(os.Environ())
	cmd.Stdout = f
	cmd.Stderr = f
	if err := cmd.Start(); err != nil {
		return nil, err
	}
	return &Spare{
		cmd:       cmd,
		pid:       cmd.Process.Pid,
		sock:      sock,
		spawnedAt: time.Now(),
		log:       log,
	}, nil
}

// Adopt takes ownership of a Claude Code spare that this process did not start
// — for example one pre-booted by `environment-runner preload-claude` during
// container startup.
//
// Binary: 0x21a93c0 - TaVHwGAw.ZwGF_8x_DFg1, whose only recovered literal is
// the log line below (0x21a947e, len 44). The candidate PIDs come from
// TaVHwGAw.DJVkMcHNhiu (0x21a8440), which references PreloadFlag twice and
// whose func10 (0x21a932a) calls the liveness check.
//
// TODO(re): DJVkMcHNhiu's enumeration source (a /proc walk vs. a pidfile) is
// not reconstructed; only that it filters on PreloadFlag and liveness.
func Adopt(log *slog.Logger, pid int, sock string) *Spare {
	log.Info("claude-preload adopted from external preload")
	return &Spare{
		pid:       pid,
		sock:      sock,
		spawnedAt: time.Now(),
		adopted:   true,
		log:       log,
	}
}

// alive reports whether the spare process is still a live Claude Code preload.
//
// Binary: 0x21ad060 - TaVHwGAw.(*Qx7xZhVlaq46).pL1QBZl_VraK
//
// Body (calls, in order): fmt.Sprintf -> os.ReadFile -> slicebytetostring ->
// strings/strconv helper -> TaVHwGAw.cXojYec2BlP -> slog.
// Literals: "/proc/%d/stat" (0x21ad072) and, on the failure path,
// "adopted spare pid failed identity re-check; treating as dead" (0x21ad35b).
//
// The identity re-check exists because an adopted PID is not ours: between
// discovery and claim the process can exit and the PID be recycled, so a live
// /proc entry alone is not enough.
func (s *Spare) alive() bool {
	statPath := fmt.Sprintf("/proc/%d/stat", s.pid)
	b, err := os.ReadFile(statPath)
	if err != nil {
		return false
	}
	// TODO(re): the field extracted from /proc/<pid>/stat is not pinned down.
	// The body converts the bytes to a string and hands them to a
	// strings/strconv helper; the natural reading is the process state field
	// (index 2), treating "Z"/"X" as dead.
	if isDeadState(string(b)) {
		return false
	}
	if s.adopted && !sameClaudePreload(s.pid) {
		s.log.Warn("adopted spare pid failed identity re-check; treating as dead")
		return false
	}
	return true
}

// TODO(re): stub — see alive(); the exact /proc/<pid>/stat parse is unrecovered.
func isDeadState(stat string) bool {
	fields := strings.Fields(stat)
	if len(fields) < 3 {
		return true
	}
	return fields[2] == "Z" || fields[2] == "X"
}

// sameClaudePreload verifies that the PID still names a `claude --preload`
// process.
//
// Binary: 0x21a9940 - TaVHwGAw.cXojYec2BlP
// Literals: "/proc/%d/cmdline" (0x21a994f), PreloadFlag (0x21a9bd9),
// "/proc/%d/comm" (0x21a9ccd).
// Calls: fmt.Sprintf x2, os.ReadFile x2, slicebytetostring x2, strings helpers,
// runtime.memequal.
//
// TODO(re): the exact comparison against /proc/<pid>/comm (equality with
// "claude" vs. a suffix test) is not recovered — the compared literal lives in
// the caller, not in this function.
func sameClaudePreload(pid int) bool {
	cmdline, err := os.ReadFile(fmt.Sprintf("/proc/%d/cmdline", pid))
	if err != nil {
		return false
	}
	if !containsArg(string(cmdline), PreloadFlag) {
		return false
	}
	comm, err := os.ReadFile(fmt.Sprintf("/proc/%d/comm", pid))
	if err != nil {
		return false
	}
	// TODO(re): comparand not recovered; "claude" is the only value consistent
	// with the binary this spawns.
	return strings.TrimSpace(string(comm)) == "claude"
}

// TODO(re): stub — /proc/<pid>/cmdline is NUL-separated; the binary splits it
// before comparing, but the separator literal was not recovered.
func containsArg(cmdline, arg string) bool {
	for _, a := range strings.Split(cmdline, "\x00") {
		if a == arg {
			return true
		}
	}
	return false
}

// WireOutput attaches the spare's stdout/stderr to the session's log tailer once
// the spare has been claimed.
//
// Binary: 0x21ab340 - TaVHwGAw.(*Qx7xZhVlaq46).WireOutput, with two goroutine
// closures each holding a deferred cleanup (WireOutput.func1/.func2 at
// 0x21ab640/0x21ab4c0 and their deferwrap1s).
//
// TODO(re): not reconstructed — no literals and no distinctive calls were
// recovered from the body.
func (s *Spare) WireOutput(stdout, stderr *os.File) error {
	// TODO(re): stub — copies the spare's output into the session tailer.
	return errors.New("TODO(re): WireOutput not reconstructed")
}

// Wait reaps the spare process.
//
// Binary: 0x21ae1c0 - TaVHwGAw.(*Qx7xZhVlaq46).Wait (+ Wait.func1 0x21ae5c0 and
// Wait.deferwrap1 0x21ae560).
//
// TODO(re): not reconstructed; the forced-execution probe of its literal block
// did not reach a materialisation point.
func (s *Spare) Wait() error {
	// TODO(re): stub.
	return errors.New("TODO(re): Wait not reconstructed")
}

// Kill terminates an unclaimed or failed spare.
//
// Binary: 0x21ae600 - TaVHwGAw.(*Qx7xZhVlaq46).Kill. Called from
// TaVHwGAw.(*EAT9SthH).Execute at 0x2172cac (immediately after the claim-miss
// is recorded, i.e. a spare that could not be claimed is killed rather than
// left running) and from the session Run loop at 0x24607c0 / 0x2473700
// (l2uwXm6g2pDF.(*W6O3FbYja2cf).Run and its deferwrap).
func (s *Spare) Kill() error {
	// TODO(re): stub — the body was not reconstructed; the natural reading is
	// signal + reap of s.pid.
	if s.cmd != nil && s.cmd.Process != nil {
		return s.cmd.Process.Kill()
	}
	return syscall.Kill(s.pid, syscall.SIGKILL)
}

// NormalizeEnv is the environment helper shared by preload-claude, Spawn and
// the executor.
//
// Binary: 0x2159d40 - TaVHwGAw.MuFihOu2FLD. It walks a []string and splits each
// entry at the first "=" via internal/stringslite.Cut.
//
// Observed behaviour: with a controlled 5-variable environment the output is
// byte-identical to the input, so it neither injects nor removes variables in
// that case.
//
// TODO(re): the actual transformation (dedupe last-wins? drop a particular
// key?) is not determined — no input exercised a difference.
func NormalizeEnv(env []string) []string {
	// TODO(re): stub — identity is all that has been observed.
	return env
}
