package main

import (
	"context"
	"net/http"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/linkage_servicepb"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

const actionClientMetadataPath = "/oauth/client-metadata.json"

// actionClientMetadata serves the Action Service's public client document without owning its configuration.
func actionClientMetadata(client linkage_servicepb.LinkageServiceClient) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
		defer cancel()
		document, err := client.GetClientMetadata(ctx, &linkage_servicepb.GetClientMetadataRequest{})
		switch status.Code(err) {
		case codes.OK:
		case codes.NotFound:
			http.NotFound(w, r)
			return
		default:
			http.Error(w, "the Action Service is temporarily unavailable", http.StatusServiceUnavailable)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("Cache-Control", "public, max-age=3600")
		_, _ = w.Write(document.GetJsonDocument())
	})
}
