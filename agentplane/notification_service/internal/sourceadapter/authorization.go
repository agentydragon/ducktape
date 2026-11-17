package sourceadapter

import (
	"context"
	"database/sql"
	"errors"

	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/publication"
	githubstate "github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/state"
	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/transaction"
	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	"github.com/google/uuid"
)

// Authorization is the closed set of successful source authorizations.
type Authorization interface{ isAuthorization() }

type (
	ActionsAuthorization struct{}
	GitHubAuthorization  struct{ Access githubstate.AccessKey }
)

func (ActionsAuthorization) isAuthorization() {}
func (GitHubAuthorization) isAuthorization()  {}

func CheckAuthorization(source Source, authorization Authorization) error {
	switch source.(type) {
	case ActionsSource:
		if _, ok := authorization.(ActionsAuthorization); !ok {
			return errors.New("an Action subscription needs Action authorization")
		}
	case GitHubSource:
		authorized, ok := authorization.(GitHubAuthorization)
		if !ok {
			return errors.New("a GitHub subscription is bound to the access it was authorized under")
		}
		if authorized.Access.AppID <= 0 || authorized.Access.InstallationID <= 0 || authorized.Access.RepositoryID <= 0 {
			return model.Invalid("GitHub access names an App, installation and repository above 0")
		}
	default:
		return model.Invalid("source must be an Action request or a GitHub subject")
	}
	return nil
}

// LockSubscriptionTx precedes the inbox lock for sources that share ingress state.
func LockSubscriptionTx(ctx context.Context, tx transaction.Tx, source Source, authorization Authorization) error {
	if err := CheckAuthorization(source, authorization); err != nil {
		return err
	}
	switch authorization.(type) {
	case ActionsAuthorization:
		return nil
	case GitHubAuthorization:
		return githubstate.Lock(ctx, tx)
	default:
		return errors.New("unsupported source authorization")
	}
}

// BindSubscriptionTx runs only after idempotency replay and quota checks.
func BindSubscriptionTx(ctx context.Context, tx transaction.Tx, source Source, authorization Authorization) (model.SubscriptionBinding, error) {
	if err := CheckAuthorization(source, authorization); err != nil {
		return model.SubscriptionBinding{}, err
	}
	switch source := source.(type) {
	case ActionsSource:
		return model.SubscriptionBinding{ActionsAfterSequence: sql.NullInt64{Int64: source.AfterSequence, Valid: true}}, nil
	case GitHubSource:
		access := authorization.(GitHubAuthorization).Access
		subject, err := githubstate.NewSubjectKey(access.RepositoryID, source.Subject)
		if err != nil {
			return model.SubscriptionBinding{}, err
		}
		if err := githubstate.GrantTx(ctx, tx, access, source.Repository); err != nil {
			return model.SubscriptionBinding{}, err
		}
		return publication.BindSubscriptionTx(ctx, tx, access.AppID, access.InstallationID, access.RepositoryID, subject.Kind, subject.Key)
	default:
		return model.SubscriptionBinding{}, model.Invalid("source must be an Action request or a GitHub subject")
	}
}

func InitializeSubscriptionTx(ctx context.Context, tx transaction.Tx, id uuid.UUID, source Source, authorization Authorization) error {
	if err := CheckAuthorization(source, authorization); err != nil {
		return err
	}
	switch source := source.(type) {
	case ActionsSource:
		return nil
	case GitHubSource:
		access := authorization.(GitHubAuthorization).Access
		subject, err := githubstate.NewSubjectKey(access.RepositoryID, source.Subject)
		if err != nil {
			return err
		}
		return publication.InitializeSubscriptionTx(ctx, tx, id, source.Source,
			access.AppID, access.InstallationID, access.RepositoryID, subject.Kind, subject.Key)
	default:
		return model.Invalid("source must be an Action request or a GitHub subject")
	}
}
