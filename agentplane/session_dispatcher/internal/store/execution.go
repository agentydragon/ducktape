package store

import (
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	"google.golang.org/protobuf/proto"
)

// SessionExecution is the exact Identity incarnation a Session executes as.
type SessionExecution struct{ Identity *identitypb.Identity }

func (s *Session) Execution() SessionExecution {
	return SessionExecution{Identity: proto.Clone(s.ExecutionIdentity).(*identitypb.Identity)}
}
