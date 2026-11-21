package sourceadapter

import (
	"errors"

	githubmodel "github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/model" // Distinguishes GitHub-owned types from common notification model types.
	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
)

// Source is the closed set of subscription sources accepted at the Notifications boundary.
type Source interface{ isSource() }

// ActionsSource and GitHubSource wrap values owned by their source domains.
type (
	ActionsSource struct{ model.ActionsSource }
	GitHubSource  struct{ githubmodel.Source }
)

func (ActionsSource) isSource() {}
func (GitHubSource) isSource()  {}

// WrapActionsSource and WrapGitHubSource create the closed source variants.
func WrapActionsSource(value model.ActionsSource) ActionsSource { return ActionsSource{value} }
func WrapGitHubSource(value githubmodel.Source) GitHubSource    { return GitHubSource{value} }

// Subscribe holds the immutable creation parameters compared on an identical retry.
type Subscribe struct {
	SessionID      string
	IdempotencyKey string
	Source         Source
	LifetimeDays   int64
}

// Validate checks common creation fields and delegates source-owned validation.
func (s Subscribe) Validate() error {
	if err := errors.Join(
		model.CheckText("session_id", s.SessionID, 1, 200),
		model.CheckText("idempotency_key", s.IdempotencyKey, 1, 200),
	); err != nil {
		return err
	}
	if s.LifetimeDays < 1 || s.LifetimeDays > 30 {
		return model.Invalid("lifetime_days must be 1 to 30")
	}
	switch source := s.Source.(type) {
	case ActionsSource:
		if source.AfterSequence < 0 || source.AfterSequence > 1<<31-1 {
			return model.Invalid("after_sequence must be 0 to 2147483647")
		}
		return nil
	case GitHubSource:
		return source.Source.Validate()
	default:
		return model.Invalid("source must be an Action request or a GitHub subject")
	}
}

// Equal reports whether two creations name the same subscription.
func (s Subscribe) Equal(other Subscribe) bool {
	return s.SessionID == other.SessionID &&
		s.IdempotencyKey == other.IdempotencyKey && s.LifetimeDays == other.LifetimeDays &&
		SourcesEqual(s.Source, other.Source)
}

// SourcesEqual reports whether two closed source selections have the same identity.
func SourcesEqual(a, b Source) bool {
	switch a := a.(type) {
	case ActionsSource:
		b, ok := b.(ActionsSource)
		return ok && a.ActionsSource == b.ActionsSource
	case GitHubSource:
		b, ok := b.(GitHubSource)
		return ok && a.Source.Equal(b.Source)
	default:
		return false
	}
}
