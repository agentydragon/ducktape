// Reconstructed from binary: environment-manager (Build ID 0b86a2a0, version
// release-1186d93b9-ext). NEW in this build — absent from the previous stored
// reference (release-d84d76b7-ext).
//
// Source: cmd/cmd_preload_claude.go
// Package: github.com/anthropics/anthropic/api-go/environment-manager/cmd
//
// Identification: main.main calls 7 constructors from the garbled cmd package
// `qqGXzsqMa` in this build vs 6 in the old one. Matching each constructor to a
// subcommand by flag arity (number and type of pflag registrations) gives an
// exact bijection:
//
//	qqGXzsqMa.Y1iOhMzA9   11 StringVar + 7 BoolVarP                 -> task-run   (18 flags)
//	qqGXzsqMa.QOacYf1     11 StringVar + 5 DurationVar + 2 IntVar
//	                       + 2 BoolVarP                             -> orchestrator (20)
//	qqGXzsqMa.UYxV8m_2y    5 StringVar + 2 BoolVarP                 -> setup      (7)
//	qqGXzsqMa.CzcVRdf3     6 StringVar + 1 IntVar                   -> poll       (7)
//	qqGXzsqMa.WDDjo1zSLZo  no flags                                 -> code-sign / print-sandbox-settings
//	qqGXzsqMa.HWV7zqp      no flags                                 -> the other of those two
//	qqGXzsqMa.EVAXAr5      1 StringVar                              -> preload-claude
//
// The old cmd package `FgSB6rLPg` has flag arities {0, 0, 20, 7, 7, 18} — the
// single-StringVar constructor has no counterpart there, so EVAXAr5 is the new
// one. preload-claude is the only subcommand with exactly one flag.

package cmd

import (
	"context"
	"fmt"
	"log/slog"
	"os"
	"os/exec"
	"os/signal"
	"syscall"

	"github.com/anthropics/anthropic/api-go/environment-manager/internal/claude"
	"github.com/anthropics/anthropic/api-go/environment-manager/internal/spare"
	"github.com/spf13/cobra"
)

// AddPreloadClaudeCommand registers the "preload-claude" subcommand.
//
// Binary: 0x24f2740 - qqGXzsqMa.EVAXAr5
//
// Parameters:
//
//	AX = *cobra.Command (root command); ends in (*Command).AddCommand at 0x24f31c0.
//
// Literals recovered by forced execution of the garble -literals decryption
// blocks (garble was built with -literals, so none of these appear in `strings`):
//
//	0x24f2752, 0x24f2771 -> "preload-claude"                     (Use, and the help index entry)
//	0x24f28b5            -> "Pre-boot a Claude Code spare for a later task-run to claim"
//	0x24f2abd, 0x24f2b07 -> "claude-path"
//	0x255fb00 (EVAXAr5.func5, outlined literal, 63-byte buffer)
//	                     -> "Path to claude binary (default: resolved via GetClaudePath)"
//
// The command has no Long, no Example and no Args validator: the live
// `preload-claude --help` prints only Short, Usage and the two flags.
func AddPreloadClaudeCommand(rootCmd *cobra.Command) {
	// Binary: 0x24f2765, 0x24f2782 - runtime.newobject for the flag storage
	// and the cobra.Command.
	var claudePath string

	preloadCmd := &cobra.Command{
		Use:   "preload-claude",
		Short: "Pre-boot a Claude Code spare for a later task-run to claim",
		RunE: func(cmd *cobra.Command, args []string) error {
			return runPreloadClaude(claudePath)
		},
	}

	// Binary: 0x24f2ba0 (*cobra.Command).Flags, 0x24f3190 (*pflag.FlagSet).StringVar.
	// Register args read off the Go register ABI at the call site:
	//   AX = *pflag.FlagSet, BX = &claudePath, CX/DI = "claude-path",
	//   SI/R8 = "" (empty default), R9/R10 = usage (from EVAXAr5.func5).
	preloadCmd.Flags().StringVar(&claudePath, "claude-path", "",
		"Path to claude binary (default: resolved via GetClaudePath)")

	// Binary: 0x24f31c0
	rootCmd.AddCommand(preloadCmd)
}

// runPreloadClaude pre-boots a Claude Code process in "spare" mode and blocks
// until it exits. The spare publishes its own claim endpoint — a Unix domain
// socket at spare.SocketPath — and a later `task-run` claims it over that
// socket (see internal/spare).
//
// Binary: 0x24f33c0 - qqGXzsqMa.EVAXAr5.func3 (the RunE closure)
//
// Verified end-to-end against the live binary. With --claude-path pointed at a
// harmless stub, strace shows exactly one execve and no other filesystem or
// network activity from the environment-manager process itself:
//
//	execve("<claude-path>", ["<claude-path>", "--preload",
//	                         "/home/claude/.claude/remote/spare.sock"], environ)
//
// and `--claude-path /bin/echo` prints
//
//	--preload /home/claude/.claude/remote/spare.sock
//
// on the parent's stdout, confirming Args, Stdout and the inherited environment.
// environment-manager creates no directory, no file and no socket here: the
// socket is bound by the claude process, not by this command.
func runPreloadClaude(claudePath string) error {
	// Binary: 0x24f344a - signal.NotifyContext(context.Background(),
	//   syscall.SIGINT, syscall.SIGTERM). The two-element []os.Signal is built
	//   at 0x24f340d-0x24f3440 from itab 0x2bae948 (syscall.Signal) with data
	//   pointers 0x2ba1b30 and 0x2ba1d98, whose .rodata contents are 0x02 and
	//   0x0f — SIGINT and SIGTERM.
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	// Binary: 0x24f3546-0x24f359c - a *slog.Logger is constructed and stored
	// into the package-level logger global at 0x3c1c020.
	// TODO(re): the handler options (level, output) are not reconstructed; the
	// command exposes no --log-level flag, so this is presumably the process
	// default handler.
	log := slog.Default()

	// Binary: 0x24f35ab-0x24f35e7
	//   MOVQ 0x168(SP),CX / MOVQ 0x8(CX),DX / TESTQ DX,DX / JNE skip
	// i.e. the flag value is used verbatim when non-empty, otherwise resolved.
	// Binary: 0x24f35d4 -> TaVHwGAw.DmttrbQxfow = claude.GetClaudePath
	// (identified by the flag's own help text, "default: resolved via
	// GetClaudePath", and by its body: slog + os stat/lookup of a claude path).
	if claudePath == "" {
		claudePath, _ = claude.GetClaudePath(log, ctx, nil)
	}

	// Binary: 0x24f35ff-0x24f399f build "--preload" (0x24f39b5, len 9) and
	// 0x24f39c7-0x24f3b28 build the socket path (len 38); both were read out of
	// a live process stopped at 0x24f3ba0, immediately before the call:
	//   arg[1] = "--preload"
	//   arg[2] = "/home/claude/.claude/remote/spare.sock"
	// The path is a hard-coded literal, NOT derived from $HOME: running with
	// HOME=/tmp/fakehome produces the identical string.
	//
	// Binary: 0x24f3ba0 - exec.CommandContext(ctx, claudePath, args...)
	//   AX/BX = ctx, CX/DI = claudePath, SI = &args[0], R8 = R9 = 2.
	cmd := exec.CommandContext(ctx, claudePath, "--preload", spare.SocketPath)

	// Binary: 0x24f3bae syscall.Environ, 0x24f3bb3 TaVHwGAw.MuFihOu2FLD,
	// stored into Cmd.Env at 0x24f3be1/0x24f3bc0/0x24f3bc4.
	// MuFihOu2FLD splits each entry on "=" (internal/stringslite.Cut) and is
	// shared with the spare spawner. Observed behaviour: with a controlled
	// 5-variable environment the resulting Cmd.Env is byte-identical to
	// os.Environ(), so it neither adds nor drops variables in that case.
	// TODO(re): MuFihOu2FLD's exact contract (dedupe last-wins? drop a specific
	// key?) is not pinned down — only its identity behaviour is observed.
	cmd.Env = spare.NormalizeEnv(os.Environ())

	// Binary: 0x24f3be5-0x24f3c2e - Cmd.Stdout (offset 0x60/0x68) and
	// Cmd.Stderr (0x70/0x78) both get itab 0x2ba9fa0 (*os.File). Confirmed at
	// runtime: the stub's stdout arrives on the parent's stdout. Cmd.Stdin is
	// left nil, so os/exec opens /dev/null (seen in strace).
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr

	// Binary: 0x24f3c4a - LEAQ qqGXzsqMa.EVAXAr5.func3.3 into the Cmd.
	// func3.3 (0x24f4000) loads Cmd.Process (offset 0xa0), returns nil when it
	// is nil, otherwise calls (*os.Process).Signal with the syscall.Signal at
	// 0x2ba1d98 = 0x0f = SIGTERM. That is exec.Cmd.Cancel, so cancelling ctx
	// (SIGINT/SIGTERM to this process) forwards SIGTERM to the spare rather
	// than the default SIGKILL.
	cmd.Cancel = func() error {
		if cmd.Process == nil {
			return nil
		}
		return cmd.Process.Signal(syscall.SIGTERM)
	}

	// Binary: 0x24f3ca5 - (*exec.Cmd).Run. Run, not Start: preload-claude runs
	// the spare in the foreground and only returns once it exits. Verified:
	// with --claude-path /bin/true the command exits 0 immediately.
	if err := cmd.Run(); err != nil {
		// Binary: 0x24f3e4a - literal "claude-preload exited: %w"; 0x24f3f18 is
		// the fmt.Errorf. Verified by running with --claude-path /bin/false:
		//   Error: claude-preload exited: exit status 1
		return fmt.Errorf("claude-preload exited: %w", err)
	}

	return nil
}

// TODO(re): two further literal blocks on error paths of EVAXAr5.func3 were not
// reached by any observed run and are not reconstructed:
//   - 0x24f3c33 (materialises at 0x24f3cd9)
//   - 0x24f3cf7 (materialises at 0x24f3ed1)
//
// They sit between the Cmd construction and the Run error wrap, so they are
// most likely a log message and a second error format string.
