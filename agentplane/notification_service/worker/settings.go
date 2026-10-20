package worker

import (
	"errors"
	"time"
)

// Settings are the worker's tunables, as the service's settings file holds them: the service's config embeds
// this struct under the keys its mapstructure tags name.
type Settings struct {
	NoticeDebounce NoticeDebounce `mapstructure:"notice_debounce"`
	// StaleInboxConfirmationSeconds is the wait between the first lookup that finds an inbox's Sandbox gone or
	// replaced and the one that retires the inbox.
	StaleInboxConfirmationSeconds float64 `mapstructure:"stale_inbox_confirmation_s"`
}

// NoticeDebounce sets when entries are announced to the agent. Persistence and inbox reads are never delayed.
type NoticeDebounce struct {
	// QuietSeconds is the wait after the newest unannounced entry before a notice is prepared. Zero disables
	// the debounce.
	QuietSeconds float64 `mapstructure:"quiet_seconds"`
	// MaxWaitSeconds caps the wait from the oldest unannounced entry, however busy the inbox is. It does not
	// bound runner outages or delivery retries.
	MaxWaitSeconds float64 `mapstructure:"max_wait_seconds"`
}

func DefaultSettings() Settings {
	return Settings{
		NoticeDebounce:                NoticeDebounce{QuietSeconds: 2, MaxWaitSeconds: 10},
		StaleInboxConfirmationSeconds: 30,
	}
}

// Validate checks the bounds the Python service's settings enforce.
func (s Settings) Validate() error {
	var failures []error
	if s.NoticeDebounce.QuietSeconds < 0 || s.NoticeDebounce.QuietSeconds > 3600 {
		failures = append(failures, errors.New("notice_debounce.quiet_seconds must be 0 to 3600"))
	}
	if s.NoticeDebounce.MaxWaitSeconds <= 0 || s.NoticeDebounce.MaxWaitSeconds > 3600 {
		failures = append(failures, errors.New("notice_debounce.max_wait_seconds must be above 0 and at most 3600"))
	}
	if s.StaleInboxConfirmationSeconds <= 0 || s.StaleInboxConfirmationSeconds > 3600 {
		failures = append(failures, errors.New("stale_inbox_confirmation_s must be above 0 and at most 3600"))
	}
	return errors.Join(failures...)
}

func seconds(value float64) time.Duration { return time.Duration(value * float64(time.Second)) }
