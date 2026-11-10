package sessionidentity

import (
	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	"github.com/agentydragon/ducktape/agentplane/notification_service/store"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/session_dispatcherpb"
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

// AccountUID accepts only the identity established for this exact Session placement.
// The current same-name Kubernetes account cannot establish historical ownership.
func AccountUID(session *dispatcherpb.Session, sessionID string, ref model.DestinationRef, account model.ServiceAccount) (string, error) {
	target := Target(session, sessionID)
	identity := session.GetExecutionIdentity()
	if target == nil || target.GetName() != ref.Name || target.GetUid() != ref.UID ||
		identity.GetAccount().GetNamespace() != ref.Namespace || identity.GetAccount().GetNamespace() != account.Namespace ||
		identity.GetAccount().GetName() != account.Name || identity.GetUid() == "" {
		return "", store.ErrNotFound
	}
	return identity.GetUid(), nil
}

// Identity is the Identity incarnation the Session authority recorded this exact Session placement as
// executing as; ok is false when it recorded none or the placement is another Sandbox's.
func Identity(session *dispatcherpb.Session, sessionID string, ref model.DestinationRef) (identity model.Identity, ok bool) {
	target := Target(session, sessionID)
	recorded := session.GetIdentity()
	if target == nil || target.GetName() != ref.Name || target.GetUid() != ref.UID ||
		recorded.GetName() == "" || recorded.GetUid() == "" {
		return model.Identity{}, false
	}
	return model.Identity{Name: recorded.GetName(), UID: recorded.GetUid()}, true
}
