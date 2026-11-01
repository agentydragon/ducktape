package runtime

import (
	"fmt"
	"io"
	"net"
	"sync"
	"testing"

	ingresspb "github.com/agentydragon/ducktape/agentplane/history_service/session_ingresspb"
	"google.golang.org/grpc"
	"google.golang.org/protobuf/proto"
)

// The lifecycle fixture needs a real ingress acknowledgement stream: Runtime
// cannot finish a sealed drain while Host's journals have no acknowledged seal.
func startAcknowledgingHistory(t *testing.T) string {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer()
	ingresspb.RegisterSessionJournalIngressServer(server, &acknowledgingHistory{acks: map[string]*ingresspb.Acknowledgement{}})
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(server.Stop)
	return listener.Addr().String()
}

type acknowledgingHistory struct {
	ingresspb.UnimplementedSessionJournalIngressServer
	mu   sync.Mutex
	acks map[string]*ingresspb.Acknowledgement
}

func (h *acknowledgingHistory) Push(stream grpc.BidiStreamingServer[ingresspb.PushFrame, ingresspb.Acknowledgement]) error {
	first, err := stream.Recv()
	if err != nil {
		return err
	}
	announce := first.GetAnnounce()
	if announce == nil || announce.SessionId == "" {
		return fmt.Errorf("History requires an initial Session announcement")
	}
	key := announce.SessionId + "\x00" + announce.GetIncarnationUid()
	h.mu.Lock()
	ack := h.acks[key]
	if ack == nil {
		ack = &ingresspb.Acknowledgement{}
		h.acks[key] = ack
	}
	snapshot := proto.Clone(ack).(*ingresspb.Acknowledgement)
	h.mu.Unlock()
	if err := stream.Send(snapshot); err != nil {
		return err
	}
	for {
		frame, err := stream.Recv()
		if err == io.EOF {
			return nil
		}
		if err != nil {
			return err
		}
		h.mu.Lock()
		ack = h.acks[key]
		if entry := frame.GetEntry(); entry != nil {
			if ack.Sealed || entry.Cursor != ack.Cursor+1 {
				h.mu.Unlock()
				return fmt.Errorf("History received an entry outside the contiguous unsealed prefix")
			}
			ack.Cursor = entry.Cursor
		} else if seal := frame.GetSealed(); seal != nil {
			if seal.Cursor != ack.Cursor || seal.GetIncarnationUid() == "" {
				h.mu.Unlock()
				return fmt.Errorf("History seal does not name the received prefix and incarnation")
			}
			ack.Sealed = true
		} else if frame.GetHeartbeat() == nil {
			h.mu.Unlock()
			return fmt.Errorf("unexpected History ingress frame")
		}
		snapshot = proto.Clone(ack).(*ingresspb.Acknowledgement)
		h.mu.Unlock()
		if err := stream.Send(snapshot); err != nil {
			return err
		}
	}
}
