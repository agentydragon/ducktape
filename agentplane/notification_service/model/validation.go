package model

import (
	"errors"
	"fmt"
	"regexp"
	"unicode/utf8"
)

var (
	repositoryPattern = regexp.MustCompile(`^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$`)
	actionPattern     = regexp.MustCompile(`^[a-z_]+$`)
	shaPattern        = regexp.MustCompile(`^[0-9a-f]{40}$`)
)

// ErrInvalid marks a request that breaks a documented bound or pattern.
var ErrInvalid = errors.New("invalid argument")

// Invalid formats a validation error that keeps the shared invalid-argument identity.
func Invalid(format string, args ...any) error {
	return fmt.Errorf("%w: %s", ErrInvalid, fmt.Sprintf(format, args...))
}

// CheckText validates UTF-8 and a rune-count bound for a named field.
func CheckText(field, value string, minLength, maxLength int) error {
	if !utf8.ValidString(value) {
		return Invalid("%s is not valid UTF-8", field)
	}
	if length := utf8.RuneCountInString(value); length < minLength || length > maxLength {
		return Invalid("%s must be %d to %d characters", field, minLength, maxLength)
	}
	return nil
}

// IsCommitSHA reports whether value is a lowercase 40-character Git commit SHA.
func IsCommitSHA(value string) bool {
	return shaPattern.MatchString(value)
}

// IsRepositoryName reports whether value is the bounded owner/name accepted by GitHub sources.
func IsRepositoryName(value string) bool {
	return len(value) <= 200 && repositoryPattern.MatchString(value)
}
