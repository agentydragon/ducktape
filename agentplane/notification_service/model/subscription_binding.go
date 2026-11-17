package model

import "database/sql"

// SubscriptionBinding holds the existing source-binding columns of a subscription row.
type SubscriptionBinding struct {
	ActionsAfterSequence                            sql.NullInt64
	GitHubStartPosition                             sql.NullInt64
	GitHubCommitAfterSequence                       sql.NullInt64
	GitHubPRStateAfterSequence                      sql.NullInt64
	GitHubApp, GitHubInstallation, GitHubRepository sql.NullInt64
	GitHubSubjectKind, GitHubSubjectKey             sql.NullString
}
