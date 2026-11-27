package main

import (
	"github.com/agentydragon/ducktape/agentplane/servicemain"
	"github.com/agentydragon/ducktape/agentplane/session_dispatcher/internal/app"
)

func main() {
	servicemain.Main(app.Service())
}
