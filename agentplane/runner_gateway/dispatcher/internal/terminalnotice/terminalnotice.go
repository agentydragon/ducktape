package terminalnotice

import (
	"encoding/base64"
	"errors"
	"fmt"
	"sync"
	"time"

	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/api/session/session_dispatcherpb"
	"github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/internal/notification"
	"github.com/google/uuid"
	"google.golang.org/protobuf/proto"
)

const (
	chunkBytes      = 4000
	maxParts        = 2048
	maxPending      = 32
	MaxPayloadBytes = 7900
)

// Event is a terminal dispatch snapshot sent to active WatchDispatches streams.
type Event struct {
	ID            string
	Dispatch      *dispatcherpb.Dispatch
	OwnerIdentity *identitypb.Identity
}

type assembly struct {
	started      time.Time
	count        int
	parts        map[int][]byte
	identityName string
	identityUID  string
}

// Assembler decodes and reassembles terminal NOTIFY payloads. Its zero value is ready to use.
type Assembler struct {
	mu      sync.Mutex
	pending map[string]*assembly
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

// Add accepts one encoded part and returns an event after all parts arrive.
func (a *Assembler) Add(payload string) (*Event, error) {
	encoded, err := base64.StdEncoding.Strict().DecodeString(payload)
	if err != nil {
		return nil, fmt.Errorf("invalid terminal envelope: %w", err)
	}
	item := &notification.TerminalNotificationPart{}
	if err := proto.Unmarshal(encoded, item); err != nil {
		return nil, err
	}
	if _, err := uuid.Parse(item.GetEventId()); err != nil {
		return nil, err
	}
	if item.GetPartCount() < 1 || item.GetPartCount() > maxParts || item.GetPart() >= item.GetPartCount() {
		return nil, fmt.Errorf("invalid terminal part")
	}
	data := item.GetData()
	if len(data) > chunkBytes {
		return nil, errors.New("terminal part is too large")
	}
	identityName, identityUID := item.GetExecutionIdentityName(), item.GetExecutionIdentityUid()
	if (identityName == "") != (identityUID == "") {
		return nil, errors.New("terminal notification has an incomplete Session Identity")
	}
	a.mu.Lock()
	if a.pending == nil {
		a.pending = map[string]*assembly{}
	}
	assembled := a.pending[item.GetEventId()]
	if assembled == nil {
		if len(a.pending) >= maxPending {
			a.mu.Unlock()
			return nil, errors.New("too many pending terminal events")
		}
		assembled = &assembly{
			started: time.Now(), count: int(item.GetPartCount()), parts: map[int][]byte{},
			identityName: identityName, identityUID: identityUID,
		}
		a.pending[item.GetEventId()] = assembled
	}
	if assembled.count != int(item.GetPartCount()) ||
		assembled.identityName != identityName || assembled.identityUID != identityUID {
		a.mu.Unlock()
		return nil, fmt.Errorf("terminal part count changed")
	}
	part := int(item.GetPart())
	if previous, ok := assembled.parts[part]; ok && string(previous) != string(data) {
		a.mu.Unlock()
		return nil, errors.New("terminal part changed")
	}
	assembled.parts[part] = data
	if len(assembled.parts) < assembled.count {
		a.mu.Unlock()
		return nil, nil
	}
	delete(a.pending, item.GetEventId())
	a.mu.Unlock()
	serialized := make([]byte, 0, assembled.count*chunkBytes)
	for i := 0; i < assembled.count; i++ {
		chunk, ok := assembled.parts[i]
		if !ok || (i < assembled.count-1 && len(chunk) != chunkBytes) {
			return nil, errors.New("terminal part missing or short")
		}
		serialized = append(serialized, chunk...)
	}
	dispatch := &dispatcherpb.Dispatch{}
	if err := proto.Unmarshal(serialized, dispatch); err != nil {
		return nil, err
	}
	if dispatch.GetCommandId() == "" || dispatch.GetReason() != "sandbox_deleted" ||
		(dispatch.GetState() != dispatcherpb.DeliveryState_DELIVERY_STATE_FAILED && dispatch.GetState() != dispatcherpb.DeliveryState_DELIVERY_STATE_UNKNOWN) {
		return nil, errors.New("terminal snapshot did not describe Sandbox deletion")
	}
	event := &Event{ID: item.GetEventId(), Dispatch: dispatch}
	if assembled.identityName != "" {
		event.OwnerIdentity = &identitypb.Identity{Name: assembled.identityName, Uid: assembled.identityUID}
	}
	return event, nil
}

// Reset discards incomplete events after a listener generation change.
func (a *Assembler) Reset() {
	a.mu.Lock()
	a.pending = map[string]*assembly{}
	a.mu.Unlock()
}

// HasExpired reports whether any incomplete event has exceeded timeout.
func (a *Assembler) HasExpired(now time.Time, timeout time.Duration) bool {
	a.mu.Lock()
	defer a.mu.Unlock()
	for _, event := range a.pending {
		if now.Sub(event.started) > timeout {
			return true
		}
	}
	return false
}
