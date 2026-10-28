package authserver

import (
	"context"
	"net/http"
	"os/exec"
	"testing"
	"time"

	"github.com/bazelbuild/rules_go/go/runfiles"

	"github.com/agentydragon/ducktape/agentplane/mcp_edge/mcpserver"
)

func TestPythonSDKRegistersAgainstProductionHandlers(t *testing.T) {
	h := startWithResource(t, func(server *Server) http.Handler {
		// Registration stops before authorization. The production auth middleware
		// challenges the SDK before the handler can call an Action Service.
		return mcpserver.NewHandler(nil, server.VerifyBearer, mcpserver.Options{
			ResourceMetadataURL: server.ResourceMetadataURL(),
		})
	})
	binary, err := runfiles.Rlocation("ducktape/agentplane/mcp_edge/authserver/sdk_dcr_client_bin")
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(t.Context(), 2*time.Minute)
	defer cancel()
	output, err := exec.CommandContext(ctx, binary, h.url+resourcePath).CombinedOutput()
	if err != nil {
		t.Fatalf("registration-only SDK test: %v\n%s", err, output)
	}
	var registrations int
	if err := h.db.QueryRowContext(t.Context(), `SELECT count(*) FROM oauth_registered_client`).Scan(&registrations); err != nil {
		t.Fatal(err)
	}
	if registrations != 2 {
		t.Errorf("stored %d registrations, want one for each redirect URI", registrations)
	}
}
