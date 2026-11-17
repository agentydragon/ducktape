package model

import (
	"errors"
	"fmt"
	"unicode/utf8"
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
