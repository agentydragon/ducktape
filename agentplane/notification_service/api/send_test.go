package api

import (
	"testing"
	"time"

	operatorpb "github.com/agentydragon/ducktape/agentplane/notification_service/notifications_operatorpb"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

// blockedStream is a watch whose caller has stopped reading, so a write never completes.
type blockedStream struct {
	grpc.ServerStream
	release chan struct{}
}

func (s blockedStream) Send(*operatorpb.WatchSessionNoticesResponse) error {
	<-s.release
	return nil
}

func TestAWriteTheCallerDoesNotReadFailsAtTheTimeout(t *testing.T) {
	stream := blockedStream{release: make(chan struct{})}
	t.Cleanup(func() { close(stream.release) })
	failed := make(chan error, 1)
	go func() {
		failed <- sendWithin(t.Context(), stream, 10*time.Millisecond, &operatorpb.WatchSessionNoticesResponse{})
	}()
	select {
	case err := <-failed:
		if got := status.Code(err); got != codes.DeadlineExceeded {
			t.Errorf("%v, want DeadlineExceeded", got)
		}
	case <-time.After(30 * time.Second):
		t.Fatal("the write was not given up on")
	}
}
