package main

import "github.com/agentydragon/ducktape/agentplane/env/runtime/internal/coordinator"

func newRuntime(config coordinator.Config) (*coordinator.Runtime, error) {
	return coordinator.New(config)
}
