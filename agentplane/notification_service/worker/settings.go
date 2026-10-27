package worker

import (
	"errors"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/store"
)

// Settings are the worker's tunables, as the service's settings file holds them: the service's config embeds
// this struct under the keys its mapstructure tags name.
type Settings struct {
	// GitHubDeliveryRetentionDays bounds raw receipts and generated mergeability history; nil retains both
	// indefinitely.
	GitHubDeliveryRetentionDays *int           `mapstructure:"github_delivery_retention_days"`
	NoticeDebounce              NoticeDebounce `mapstructure:"notice_debounce"`
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

// Debounce is the same waits as the store's operator reads take, so they report what the worker does.
func (d NoticeDebounce) Debounce() store.Debounce {
	return store.Debounce{Quiet: seconds(d.QuietSeconds), MaxWait: seconds(d.MaxWaitSeconds)}
}

func DefaultSettings() Settings {
	return Settings{
		NoticeDebounce:                NoticeDebounce{QuietSeconds: 2, MaxWaitSeconds: 10},
		StaleInboxConfirmationSeconds: 30,
		GitHubDeliveryRetentionDays:   new(7),
	}
}

// Validate checks the bounds of the settings.
func (s Settings) Validate() error {
	var failures []error
	if days := s.GitHubDeliveryRetentionDays; days != nil && (*days < 1 || *days > 3650) {
		failures = append(failures, errors.New("github_delivery_retention_days must be null or 1 to 3650"))
	}
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
