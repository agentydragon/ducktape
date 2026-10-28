package main

import (
	"os"

	"github.com/agentydragon/ducktape/agentplane/runner/executor"
)

func main() {
	os.Exit(executor.Main(os.Args[1:]))
}
