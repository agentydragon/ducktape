package terminalnotice

import (
	"encoding/base64"
	"fmt"

	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/api/session/session_dispatcherpb"
	"github.com/agentydragon/ducktape/agentplane/session_dispatcher/internal/notification"
	"google.golang.org/protobuf/proto"
)

const (
	chunkBytes      = 4000
	maxParts        = 2048
	MaxPayloadBytes = 7900
)

// Event is a terminal dispatch snapshot sent to active WatchDispatches streams.
type Event struct {
	ID            string
	Dispatch      *dispatcherpb.Dispatch
	OwnerIdentity *identitypb.Identity
}

// Encode deterministically serializes a terminal event into PostgreSQL-safe NOTIFY payloads.
func Encode(event Event) ([]string, error) {
	serialized, err := (proto.MarshalOptions{Deterministic: true}).Marshal(event.Dispatch)
	if err != nil {
		return nil, err
	}
	partCount := (len(serialized) + chunkBytes - 1) / chunkBytes
	if partCount == 0 {
		partCount = 1
	}
	if partCount > maxParts {
		return nil, fmt.Errorf("terminal snapshot needs %d parts", partCount)
	}
	result := make([]string, 0, partCount)
	for i := 0; i < partCount; i++ {
		start := i * chunkBytes
		end := start + chunkBytes
		if end > len(serialized) {
			end = len(serialized)
		}
		body, err := proto.Marshal(&notification.TerminalNotificationPart{
			EventId: event.ID, Part: uint32(i), PartCount: uint32(partCount), Data: serialized[start:end],
			ExecutionIdentityName: event.OwnerIdentity.GetName(),
			ExecutionIdentityUid:  event.OwnerIdentity.GetUid(),
		})
		if err != nil {
			return nil, err
		}
		payload := base64.StdEncoding.EncodeToString(body)
		if len(payload) > MaxPayloadBytes {
			return nil, fmt.Errorf("terminal notify part exceeds %d bytes", MaxPayloadBytes)
		}
		result = append(result, payload)
	}
	return result, nil
}
