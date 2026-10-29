package gateway

import (
	"os"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/testing/kubeapiserver"
)

func TestMain(m *testing.M) {
	os.Exit(kubeapiserver.Run(m, useAPIServer))
}
