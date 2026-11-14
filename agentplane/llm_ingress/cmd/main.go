package main

import (
	"os"

	"github.com/agentydragon/ducktape/agentplane/llm_ingress/internal/server"
)

func main() {
	os.Exit(server.Run())
}
