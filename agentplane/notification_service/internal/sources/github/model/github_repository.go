package model

// GitHubRepository is a repository by the numeric ID that survives a rename, with the name last seen for it.
type GitHubRepository struct {
	ID   int64
	Name string
}

// GitHubHeadRevision is a commit that a subject has been seen at, in the repository holding it, which for a
// pull request may be a fork.
type GitHubHeadRevision struct {
	Repository GitHubRepository
	SHA        string
}
