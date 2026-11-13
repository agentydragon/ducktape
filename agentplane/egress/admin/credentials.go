package admin

import (
	"errors"
	"time"

	egressadminpb "github.com/agentydragon/ducktape/agentplane/egress/egress_adminpb"
	egressrulespb "github.com/agentydragon/ducktape/agentplane/egress/egress_rulespb"
	"github.com/agentydragon/ducktape/agentplane/egress/rules"
	"github.com/agentydragon/ducktape/agentplane/grpc/streaming"
	"github.com/agentydragon/ducktape/agentplane/identity/auth/auth"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/emptypb"
)

// WatchCredentials sends an initial snapshot followed by redacted resource changes.
// It ends on loss of cache health or authorization; healthy streams have no fixed lease.
func (s *Service) WatchCredentials(_ *egressadminpb.WatchCredentialsRequest, stream grpc.ServerStreamingServer[egressadminpb.WatchCredentialsResponse]) error {
	ctx := stream.Context()
	if s.credentials == nil {
		return status.Error(codes.Unavailable, "credential inventory unavailable")
	}
	ticker := time.NewTicker(5 * time.Second)
	defer ticker.Stop()
	send := func(frame *egressadminpb.WatchCredentialsResponse) error {
		if err := auth.RevalidateStream(ctx); err != nil {
			return err
		}
		if !s.credentials.Health.Available() {
			return status.Error(codes.Unavailable, "credential inventory unavailable")
		}
		if err := streaming.SendWithin(ctx, watchWriteTimeout, stream.Send, frame); err != nil {
			if errors.Is(err, streaming.ErrSendTimeout) {
				return status.Error(codes.DeadlineExceeded, "credential watch write deadline exceeded")
			}
			return err
		}
		return nil
	}
	var last map[string]*egressrulespb.Credential
	for {
		changed := s.credentials.CredentialsChanged()
		healthChanged := s.credentials.Health.Changed()
		if !s.credentials.Health.Available() {
			return status.Error(codes.Unavailable, "credential inventory unavailable")
		}
		credentials := s.credentials.Credentials()
		next := make(map[string]*egressrulespb.Credential, len(credentials))
		snapshot := make([]*egressrulespb.Credential, 0, len(credentials))
		for _, credential := range credentials {
			if credential.UID == "" {
				return status.Error(codes.Unavailable, "credential identity unavailable")
			}
			item, err := rules.CredentialOf(credential)
			if err != nil {
				return status.Error(codes.Internal, "credential metadata invalid")
			}
			next[item.Name] = item
			snapshot = append(snapshot, item)
		}
		if last == nil {
			for len(snapshot) > 0 {
				n := min(len(snapshot), 128)
				if err := send(&egressadminpb.WatchCredentialsResponse{Frame: &egressadminpb.WatchCredentialsResponse_Snapshot{Snapshot: &egressadminpb.CredentialSnapshot{Credentials: snapshot[:n]}}}); err != nil {
					return err
				}
				snapshot = snapshot[n:]
			}
			if err := send(&egressadminpb.WatchCredentialsResponse{Frame: &egressadminpb.WatchCredentialsResponse_SnapshotComplete{SnapshotComplete: &emptypb.Empty{}}}); err != nil {
				return err
			}
		} else {
			for _, item := range snapshot {
				if previous, ok := last[item.Name]; !ok || !proto.Equal(previous, item) {
					if err := send(&egressadminpb.WatchCredentialsResponse{Frame: &egressadminpb.WatchCredentialsResponse_Upsert{Upsert: item}}); err != nil {
						return err
					}
				}
			}
			for name, item := range last {
				if _, ok := next[name]; !ok {
					if err := send(&egressadminpb.WatchCredentialsResponse{Frame: &egressadminpb.WatchCredentialsResponse_Removed{Removed: &egressadminpb.CredentialRemoval{Name: name, Uid: item.Uid}}}); err != nil {
						return err
					}
				}
			}
		}
		last = next
		select {
		case <-ctx.Done():
			return status.FromContextError(ctx.Err()).Err()
		case <-s.draining.Done():
			return nil
		case <-changed:
		case <-healthChanged:
		case <-ticker.C:
			if err := auth.RevalidateStream(ctx); err != nil {
				return err
			}
		}
	}
}
