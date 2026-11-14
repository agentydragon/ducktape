package main

import (
	"errors"
	"flag"
	"fmt"
	"io"
	"net/url"
	"os"
	"path/filepath"
	"strconv"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/execution/companion"
)

const invalidCompanionUsage = "executor companion requires PID 1, absolute socket and binary paths, an HTTP health address, and a private PID namespace"

func main() {
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
	binary := flags.String("binary", "", "Codex executable")
	healthListen := flags.String("health-listen", "", "HTTP health endpoint")
	if err := flags.Parse(args); err != nil {
		return 64
	}
	if !filepath.IsAbs(*socket) || !filepath.IsAbs(*binary) || *healthListen == "" || flags.NArg() != 0 {
		fmt.Fprintln(os.Stderr, invalidCompanionUsage)
		return 64
	}
	if err := companion.RequirePID1(); err != nil {
		fmt.Fprintln(os.Stderr, invalidCompanionUsage)
		return 64
	}
	if err := validateURL(*listen); err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 64
	}
	if err := companion.Run(companion.Config{
		Socket: *socket, Listen: *listen, Binary: *binary, HealthListen: *healthListen,
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

func validateURL(value string) error {
	u, err := url.Parse(value)
	if err != nil {
		return err
	}
	port, err := strconv.Atoi(u.Port())
	if err != nil || port < 1 || port > 65535 || u.Scheme != "ws" || u.Hostname() != "127.0.0.1" || u.User != nil || u.Path != "" || u.RawQuery != "" || u.ForceQuery || u.Fragment != "" {
		return errors.New("executor URL must be ws://127.0.0.1:<nonzero port>")
	}
	return nil
}
