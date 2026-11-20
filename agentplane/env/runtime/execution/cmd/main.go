package main

import (
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"path/filepath"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/execution/codexexecserver"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/execution/companion"
)

const invalidCompanionUsage = "executor companion requires PID 1, absolute socket and binary paths, an HTTP health address, and a private PID namespace"

func main() {
	if len(os.Args) >= 2 && os.Args[1] == "serve" {
		os.Exit(runServer(os.Args[2:]))
	}
	if len(os.Args) < 2 || os.Args[1] != "companion" {
		fmt.Fprintln(os.Stderr, "expected companion subcommand")
		os.Exit(64)
	}
	os.Exit(runCompanion(os.Args[2:]))
}

func runCompanion(args []string) int {
	flags := flag.NewFlagSet("agentplane-execution-service companion", flag.ContinueOnError)
	flags.SetOutput(io.Discard)
	socket := flags.String("socket", "", "local ownership socket")
	listen := flags.String("listen", "", "Codex WebSocket endpoint")
	binary := flags.String("binary", "", "execution service executable")
	healthListen := flags.String("health-listen", "", "HTTP health endpoint")
	limitsFile := flags.String("limits-file", "", "absolute path to execution limits JSON")
	if err := flags.Parse(args); err != nil {
		return 64
	}
	if !filepath.IsAbs(*socket) || !filepath.IsAbs(*binary) || *healthListen == "" || (*limitsFile != "" && !filepath.IsAbs(*limitsFile)) || flags.NArg() != 0 {
		fmt.Fprintln(os.Stderr, invalidCompanionUsage)
		return 64
	}
	if err := companion.RequirePID1(); err != nil {
		fmt.Fprintln(os.Stderr, invalidCompanionUsage)
		return 64
	}
	if err := codexexecserver.ValidateURL(*listen); err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 64
	}
	if err := companion.Run(companion.Config{
		Socket: *socket, Listen: *listen, Binary: *binary, HealthListen: *healthListen, LimitsFile: *limitsFile,
	}); err != nil {
		if errors.Is(err, companion.ErrNotPID1) {
			fmt.Fprintln(os.Stderr, invalidCompanionUsage)
			return 64
		}
		fmt.Fprintln(os.Stderr, err)
		return 1
	}
	return 0
}
