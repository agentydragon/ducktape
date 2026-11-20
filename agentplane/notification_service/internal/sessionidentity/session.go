package sessionidentity

import (
	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/api/session/session_dispatcherpb"
)

// Target selects the exact environment used by a native Session or the
// fixed executor currently used by a central Session.
func Target(session *dispatcherpb.Session, sessionID string) *sessionpb.ExactEnvironment {
	if session.GetSessionId() != sessionID {
		return nil
	}
	placement := session.GetPlacement()
	if placement == nil {
		return nil
	}
	var target *sessionpb.ExactEnvironment
	switch placement.Kind.(type) {
	case *sessionpb.SessionPlacement_EnvironmentHosted:
		target = placement.GetEnvironmentHosted().GetEnvironment()
	case *sessionpb.SessionPlacement_CentralSamplerV1:
		target = placement.GetCentralSamplerV1().GetExecutorEnvironment()
	}
	if target == nil || target.GetName() == "" || target.GetUid() == "" {
		return nil
	}
	return target
}

// RecordedIdentity is the exact Identity incarnation the Session authority recorded this Session as executing as,
// regardless of its current placement.
func RecordedIdentity(session *dispatcherpb.Session, sessionID string) (identity model.Identity, ok bool) {
	if session.GetSessionId() != sessionID {
		return model.Identity{}, false
	}
	recorded := session.GetIdentity()
	if recorded.GetName() == "" || recorded.GetUid() == "" {
		return model.Identity{}, false
	}
	return model.Identity{Name: recorded.GetName(), UID: recorded.GetUid()}, true
}

// PlacedIdentity is the Session's recorded execution Identity when it has a complete current placement.
func PlacedIdentity(session *dispatcherpb.Session, sessionID string) (model.Identity, bool) {
	if Target(session, sessionID) == nil {
		return model.Identity{}, false
	}
	return RecordedIdentity(session, sessionID)
}

// Identity is the Identity incarnation the Session authority recorded this exact Session placement as
// executing as; ok is false when it recorded none or the placement is another Sandbox's.
func Identity(session *dispatcherpb.Session, sessionID string, ref model.DestinationRef) (identity model.Identity, ok bool) {
	target := Target(session, sessionID)
	identity, ok = RecordedIdentity(session, sessionID)
	if !ok || target == nil || target.GetName() != ref.Name || target.GetUid() != ref.UID {
		return model.Identity{}, false
	}
	return identity, true
}
