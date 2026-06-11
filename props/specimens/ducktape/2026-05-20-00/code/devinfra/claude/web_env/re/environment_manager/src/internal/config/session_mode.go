package config

import "fmt"

// SessionMode represents the mode for a session.
// Reconstructed from: github.com/anthropics/anthropic/api-go/environment-manager/internal/config.SessionMode
// DWARF shows it is a string typedef (str *uint8, len int) -- i.e., type SessionMode string.
//
// Session mode affects initialization behavior in anthropicEnvironmentType.Initialize:
//   - "new":          Full initialization: install languages, clone sources, run init script,
//     run "claude init" (RunInit), bootstrap skills and hooks.
//   - "setup-only":   Same as "new" — full initialization. Used for pre-warming environments
//     without starting a Claude Code session.
//   - "resume":       TODO(re): behavior unobserved. Likely runs init script (unlike
//     resume-cached), but Step 1/2 (language install, source clone) skipped.
//   - "resume-cached": Fast-resume optimization — skips Step 1 (languages), Step 3
//     (init script), and Step 2 (source clone, replaced by fast repo update).
//     The binary emits "Fast resume: Languages already installed" and
//     "Fast resume: Environment already configured" / "Skipping initialization script
//     for faster startup" log messages. Observed in /tmp/environment-manager.out with
//     has_init_script:true, confirming the skip is an explicit optimization, NOT caused
//     by an empty init_script field. Steps 4-6 (RunInit, skills, hooks) still run.
//
// Binary evidence for mode strings: "setup-only" (len 10) and "resume-cached" (len 13)
// are present in the binary string table. The Initialize method defaults to "new" if
// the session mode is empty (len == 0).
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
