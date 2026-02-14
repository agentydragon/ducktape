package config

import "fmt"

// SessionMode represents the mode for a session.
// Reconstructed from: github.com/anthropics/anthropic/api-go/environment-manager/internal/config.SessionMode
// DWARF shows it is a string typedef (str *uint8, len int) -- i.e., type SessionMode string.
type SessionMode string

const (
	SessionModeNew          SessionMode = "new"
	SessionModeSetupOnly    SessionMode = "setup-only"
	SessionModeResume       SessionMode = "resume"
	SessionModeResumeCached SessionMode = "resume-cached"
)

// String returns the string representation of the SessionMode.
// Reconstructed from: config.SessionMode.String (0x83b180)
// The value receiver simply returns the underlying string (AX,BX passthrough).
func (s SessionMode) String() string {
	return string(s)
}

// IsValid returns true if the SessionMode is one of the known valid modes.
// Reconstructed from: config.SessionMode.IsValid (0x83b0e0)
// Checks against "new" (len 3), "resume" (len 6), "setup-only" (len 10), "resume-cached" (len 13).
func (s SessionMode) IsValid() bool {
	switch s {
	case SessionModeNew, SessionModeResume, SessionModeSetupOnly, SessionModeResumeCached:
		return true
	default:
		return false
	}
}

// ParseSessionMode parses a string into a SessionMode.
// If the input is empty, it defaults to SessionModeNew.
// Returns the parsed mode and an error if the string is not a valid session mode.
// Reconstructed from: config.ParseSessionMode (0x83b1a0)
func ParseSessionMode(s string) (SessionMode, error) {
	if s == "" {
		return SessionModeNew, nil
	}

	mode := SessionMode(s)
	if mode.IsValid() {
		return mode, nil
	}

	return "", fmt.Errorf(
		"invalid session mode: %q (must be %s, %s, %s or %s)",
		s,
		SessionModeNew,
		SessionModeSetupOnly,
		SessionModeResume,
		SessionModeResumeCached,
	)
}
