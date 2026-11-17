package sourceadapter

import (
	"database/sql"

	githubstate "github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/state"
)

// DueState is the closed set of source-owned state carried by a due subscription.
type DueState interface{ isDueState() }

// ActionsDueState carries the last recorded sequence, initially the creation's after_sequence.
// A nil sequence preserves a missing stored binding for the Actions provider to reject.
type ActionsDueState struct{ AfterSequence *int64 }

// GitHubDueState carries the access authorized when the subscription was created.
type GitHubDueState struct{ Access githubstate.AccessKey }

func (ActionsDueState) isDueState() {}
func (GitHubDueState) isDueState()  {}

// DecodeDueState interprets the existing nullable source-binding columns. Missing GitHub app
// bindings remain absent so the provider reports the same unbound-subscription failure.
func DecodeDueState(source Source, afterSequence, app, installation, repository sql.NullInt64) DueState {
	switch source.(type) {
	case ActionsSource:
		var after *int64
		if afterSequence.Valid {
			after = &afterSequence.Int64
		}
		return ActionsDueState{AfterSequence: after}
	case GitHubSource:
		if app.Valid {
			return GitHubDueState{Access: githubstate.AccessKey{
				AppID: app.Int64, InstallationID: installation.Int64, RepositoryID: repository.Int64,
			}}
		}
	}
	return nil
}
