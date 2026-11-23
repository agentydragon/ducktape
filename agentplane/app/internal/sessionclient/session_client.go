// Package sessionclient adapts app Session operations to the Dispatcher.
package sessionclient

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"unicode/utf8"

	"github.com/agentydragon/ducktape/agentplane/observability"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/api/session/session_dispatcherpb"
	"github.com/google/uuid"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/reflect/protoreflect"
)

// SessionDispatcher is the narrow unary surface used by the SessionClient.
type SessionDispatcher interface {
	CreateSession(context.Context, *dispatcherpb.CreateSessionRequest) (*dispatcherpb.CreateSessionResponse, error)
}

// SessionClient adapts app Session operations to the Session Dispatcher.
type SessionClient struct {
	dispatcher SessionDispatcher
}

// New creates the app Session client.
func New(dispatcher SessionDispatcher) (*SessionClient, error) {
	if dispatcher == nil {
		return nil, errors.New("Session Dispatcher is required")
	}
	return &SessionClient{dispatcher: dispatcher}, nil
}

// CreateSession queues one immutable Session specification. The caller-minted
// UUID is the dispatcher's idempotency key, so a timeout is returned with that
// same ID for reconciliation or retry.
func (c *SessionClient) CreateSession(
	ctx context.Context, sandbox, sandboxUID string, sessionID uuid.UUID, spec *sessionpb.SessionSpec, setupScript *string,
) (*dispatcherpb.Session, error) {
	if sandbox == "" || sandboxUID == "" || spec == nil {
		return nil, errors.New("Sandbox name, UID and Session spec are required")
	}
	if setupScript != nil && utf8.RuneCountInString(*setupScript) > MaxSetupScriptCharacters {
		return nil, fmt.Errorf("setup script exceeds %d characters", MaxSetupScriptCharacters)
	}
	request := &dispatcherpb.CreateSessionRequest{
		SessionId: sessionID.String(), Spec: proto.Clone(spec).(*sessionpb.SessionSpec),
	}
	target := spec.GetPlacement().GetEnvironmentHosted().GetEnvironment()
	if spec.GetPlacement().GetCentralSamplerV1() != nil {
		target = spec.GetPlacement().GetCentralSamplerV1().GetExecutorEnvironment()
	}
	if target.GetName() != sandbox || target.GetUid() != sandboxUID {
		return nil, errors.New("Session placement does not match requested Sandbox")
	}
	if setupScript != nil {
		value := *setupScript
		request.SetupScript = &value
	}
	ctx, span := observability.Start(ctx, "agentplane.dispatcher.create_session")
	response, err := c.dispatcher.CreateSession(ctx, request)
	observability.End(span, err)
	if err != nil {
		var timeout *RequestTimeoutError
		if errors.As(err, &timeout) {
			return nil, &DispatcherRequestTimeoutError{SessionID: sessionID, Cause: err}
		}
		return nil, err
	}
	session := response.GetSession()
	var returnedTarget *sessionpb.ExactEnvironment
	if session != nil {
		returnedTarget = session.GetPlacement().GetEnvironmentHosted().GetEnvironment()
		if session.GetPlacement().GetCentralSamplerV1() != nil {
			returnedTarget = session.GetPlacement().GetCentralSamplerV1().GetExecutorEnvironment()
		}
	}
	if session == nil || session.GetSessionId() != sessionID.String() || returnedTarget.GetName() != sandbox ||
		returnedTarget.GetUid() != sandboxUID {
		return nil, &SessionNotFoundError{SessionID: sessionID, Sandbox: sandbox}
	}
	return session, nil
}

// ParseSessionSpec applies strict proto-JSON fields over a cloned set of
// defaults. Explicit empty values override defaults, and unknown fields fail.
func ParseSessionSpec(defaults *sessionpb.SessionSpec, fields map[string]any) (*sessionpb.SessionSpec, error) {
	if defaults == nil {
		defaults = &sessionpb.SessionSpec{}
	}
	merged := proto.Clone(defaults).(*sessionpb.SessionSpec)
	encoded, err := json.Marshal(fields)
	if err != nil {
		return nil, &MalformedMessageError{Cause: err}
	}
	partial := &sessionpb.SessionSpec{}
	if err := (protojson.UnmarshalOptions{DiscardUnknown: false}).Unmarshal(encoded, partial); err != nil {
		return nil, &MalformedMessageError{Cause: err}
	}
	// protojson.Unmarshal resets its destination, but request fields merge into the
	// preset. Copy only keys present in the request, including explicitly supplied
	// zero values such as "model": "".
	descriptor := merged.ProtoReflect().Descriptor().Fields()
	for name := range fields {
		field := descriptor.ByJSONName(name)
		if field == nil {
			field = descriptor.ByName(protoreflect.Name(name))
		}
		if field != nil {
			merged.ProtoReflect().Set(field, partial.ProtoReflect().Get(field))
		}
	}
	return merged, nil
}

const MaxSetupScriptCharacters = 65_536
