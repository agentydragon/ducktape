package store

import (
	"context"
	"testing"

	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/google/uuid"
)

func wakeAttribution(actor, effective *identitypb.ServiceAccountIdentity) *AdmissionAttribution {
	return &AdmissionAttribution{
		Actor: actor, EffectiveCaller: effective,
		ActorPod: &PodIdentity{Name: "actions-pod", UID: "actions-pod-uid"},
		Action:   &ActionCorrelation{RequestID: uuid.NewString(), ExecutionID: uuid.NewString()},
	}
}

func openWakeableSession(
	t *testing.T,
	store *Store,
	sessionID, sandboxName, sandboxUID string,
	actor, effective *identitypb.ServiceAccountIdentity,
) *AdmissionAttribution {
	t.Helper()
	attribution := wakeAttribution(actor, effective)
	effectiveAccount := accountOfIdentity(effective)
	if _, err := createSessionForCaller(store, context.Background(), sessionID, sandboxName, sandboxUID,
		&sessionpb.SessionSpec{}, nil, &effectiveAccount, testExecutionIdentity(), ServiceAccountCaller{Privileged: true, ServiceAccountIdentity: effective, Attribution: attribution}); err != nil {
		t.Fatal("open wakeable Session:", err)
	}
	return attribution
}
