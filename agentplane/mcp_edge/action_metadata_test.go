package main

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/action_service/linkage_servicepb"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

type metadataClient struct {
	document []byte
	err      error
}

func (c metadataClient) CompleteCallback(context.Context, *linkage_servicepb.CompleteCallbackRequest, ...grpc.CallOption) (*linkage_servicepb.CompleteCallbackResponse, error) {
	panic("metadata handler must not complete callbacks")
}

func (c metadataClient) GetClientMetadata(context.Context, *linkage_servicepb.GetClientMetadataRequest, ...grpc.CallOption) (*linkage_servicepb.GetClientMetadataResponse, error) {
	return &linkage_servicepb.GetClientMetadataResponse{JsonDocument: c.document}, c.err
}

func TestActionClientMetadata(t *testing.T) {
	for _, test := range []struct {
		name   string
		client metadataClient
		want   int
	}{
		{name: "document", client: metadataClient{document: []byte(`{"client_id":"https://example.test"}`)}, want: http.StatusOK},
		{name: "not configured", client: metadataClient{err: status.Error(codes.NotFound, "not configured")}, want: http.StatusNotFound},
		{name: "unavailable", client: metadataClient{err: status.Error(codes.Unavailable, "private detail")}, want: http.StatusServiceUnavailable},
	} {
		t.Run(test.name, func(t *testing.T) {
			recorder := httptest.NewRecorder()
			actionClientMetadata(test.client).ServeHTTP(recorder, httptest.NewRequest(http.MethodGet, actionClientMetadataPath, nil))
			if recorder.Code != test.want {
				t.Fatalf("status = %d; want %d", recorder.Code, test.want)
			}
			if test.want == http.StatusOK && (recorder.Body.String() != string(test.client.document) || recorder.Header().Get("Content-Type") != "application/json" || recorder.Header().Get("Cache-Control") != "public, max-age=3600") {
				t.Fatalf("document response = %q, headers = %v", recorder.Body.String(), recorder.Header())
			}
		})
	}
}
