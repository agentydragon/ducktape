package model

// Repository is a repository by the numeric ID that survives a rename, with the name last seen for it.
type Repository struct {
	ID   int64
	Name string
}

// HeadRevision is a commit that a subject has been seen at, in the repository holding it, which for a
// pull request may be a fork.
type HeadRevision struct {
	Repository Repository
	SHA        string
}
