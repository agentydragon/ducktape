package dispatcher

import (
	"fmt"
	"log/slog"

	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/api/session/session_dispatcherpb"
	"github.com/agentydragon/ducktape/agentplane/session_dispatcher/internal/placement"
	"github.com/agentydragon/ducktape/agentplane/session_dispatcher/internal/store"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"
)

func DescribeSession(row *store.Session) (*dispatcherpb.Session, error) {
	_, known := dispatcherpb.SessionStatus_Materialization_name[int32(row.State)]
	if !known || row.State == dispatcherpb.SessionStatus_SESSION_MATERIALIZATION_UNSPECIFIED {
		return nil, fmt.Errorf("unknown Session materialization %d", row.State)
	}
	result := &dispatcherpb.Session{
		SessionId: row.SessionID,
		Status:    &dispatcherpb.SessionStatus{Activity: describeActivity(row), Materialization: row.State, DeliveryBlocker: row.DeliveryBlocker, WakeAssessment: row.WakeAssessment},
		CreatedAt: timestamppb.New(row.CreatedAt), UpdatedAt: timestamppb.New(row.UpdatedAt),
	}
	if row.ExecutionIdentity != nil {
		result.Identity = proto.Clone(row.ExecutionIdentity).(*identitypb.Identity)
	}
	result.Status.RequiredEnvironment = dispatcherpb.SessionStatus_REQUIRED_ENVIRONMENT_AVAILABLE
	if row.EnvironmentDeletedAt != nil {
		result.Status.RequiredEnvironment = dispatcherpb.SessionStatus_REQUIRED_ENVIRONMENT_DELETED
	}
	if !row.Placement.MatchesSpec(row.Spec) {
		return nil, fmt.Errorf("stored Session placement disagrees with its spec")
	}
	result.Placement = proto.Clone(row.Spec.GetPlacement()).(*sessionpb.SessionPlacement)
	if row.Reason != "" {
		result.Status.Reason = &row.Reason
	}
	return result, nil
}

func unknownActivity(row *store.Session, reason dispatcherpb.SessionActivity_Unknown_Reason) *dispatcherpb.SessionActivity {
	return &dispatcherpb.SessionActivity{
		RecordedAt: timestamppb.New(row.CreatedAt),
		Kind:       &dispatcherpb.SessionActivity_Unknown_{Unknown: &dispatcherpb.SessionActivity_Unknown{Reason: reason}},
	}
}

func describeActivity(row *store.Session) *dispatcherpb.SessionActivity {
	if row.Activity == nil {
		return unknownActivity(row, dispatcherpb.SessionActivity_Unknown_NOT_YET_OBSERVED)
	}
	activity := &dispatcherpb.SessionActivity{}
	if err := proto.Unmarshal(row.Activity, activity); err != nil {
		slog.Warn("decode stored Session activity", "session_id", row.SessionID, "error", err)
		return unknownActivity(row, dispatcherpb.SessionActivity_Unknown_STREAM_LOST)
	}
	// A newer writer can introduce a oneof variant this reader does not know.
	if activity.Kind == nil || activity.GetRecordedAt() == nil || activity.GetRecordedAt().CheckValid() != nil {
		return unknownActivity(row, dispatcherpb.SessionActivity_Unknown_STREAM_LOST)
	}
	if observed, ok := activity.Kind.(*dispatcherpb.SessionActivity_Observed); ok {
		owner := store.ActivityOwner{Central: row.Placement == placement.CentralSampler}
		if !row.ActivityLive || observed.Observed.GetSessionId() != row.SessionID || !owner.Matches(observed.Observed) {
			return unknownActivity(row, dispatcherpb.SessionActivity_Unknown_STREAM_LOST)
		}
	}
	if activity.GetEnvironmentDown() != nil && row.Placement == placement.CentralSampler {
		return unknownActivity(row, dispatcherpb.SessionActivity_Unknown_STREAM_LOST)
	}
	if down, ok := activity.Kind.(*dispatcherpb.SessionActivity_EnvironmentDown_); ok && down.EnvironmentDown.GetCause() == dispatcherpb.SessionActivity_EnvironmentDown_SUSPENDED && !row.ActivityLive {
		return unknownActivity(row, dispatcherpb.SessionActivity_Unknown_STREAM_LOST)
	}
	return activity
}
