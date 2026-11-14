package main

import (
	"os"

	"github.com/agentydragon/ducktape/agentplane/egress/sidecar/internal/relay"
)

func main() {
	os.Exit(relay.Run())
}
