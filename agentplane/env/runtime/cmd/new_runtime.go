package main

import runtime "github.com/agentydragon/ducktape/agentplane/env/runtime"

func newRuntime(config runtime.Config) (*runtime.Runtime, error) {
	return runtime.New(config)
}
