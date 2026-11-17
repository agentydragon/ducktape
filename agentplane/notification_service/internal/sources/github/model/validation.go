package model

import "regexp"

var (
	repositoryPattern = regexp.MustCompile(`^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$`)
	actionPattern     = regexp.MustCompile(`^[a-z_]+$`)
	shaPattern        = regexp.MustCompile(`^[0-9a-f]{40}$`)
)

// IsCommitSHA reports whether value is a lowercase 40-character Git commit SHA.
func IsCommitSHA(value string) bool {
	return shaPattern.MatchString(value)
}

// IsRepositoryName reports whether value is the bounded owner/name accepted by GitHub sources.
func IsRepositoryName(value string) bool {
	return len(value) <= 200 && repositoryPattern.MatchString(value)
}
