package main

import (
	"fmt"
	"os"

	executor "github.com/agentydragon/ducktape/agentplane/env/runtime/execution/companion"
)

func main() {
	if len(os.Args) < 2 || os.Args[1] != "companion" {
		fmt.Fprintln(os.Stderr, "expected companion subcommand")
		os.Exit(64)
	}
	os.Exit(executor.Main(os.Args[2:]))
}
