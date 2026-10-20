package worker

import "testing"

func TestSettingsAreBoundedAsThePythonServiceBoundsThem(t *testing.T) {
	if err := DefaultSettings().Validate(); err != nil {
		t.Fatalf("the defaults are invalid: %v", err)
	}
	for _, c := range []struct {
		name   string
		change func(*Settings)
		valid  bool
	}{
		{"no quiet window", func(s *Settings) { s.NoticeDebounce.QuietSeconds = 0 }, true},
		{"longest quiet window", func(s *Settings) { s.NoticeDebounce.QuietSeconds = 3600 }, true},
		{"negative quiet window", func(s *Settings) { s.NoticeDebounce.QuietSeconds = -1 }, false},
		{"quiet window over an hour", func(s *Settings) { s.NoticeDebounce.QuietSeconds = 3601 }, false},
		{"longest maximum wait", func(s *Settings) { s.NoticeDebounce.MaxWaitSeconds = 3600 }, true},
		{"no maximum wait", func(s *Settings) { s.NoticeDebounce.MaxWaitSeconds = 0 }, false},
		{"maximum wait over an hour", func(s *Settings) { s.NoticeDebounce.MaxWaitSeconds = 3601 }, false},
		{"longest confirmation wait", func(s *Settings) { s.StaleInboxConfirmationSeconds = 3600 }, true},
		{"no confirmation wait", func(s *Settings) { s.StaleInboxConfirmationSeconds = 0 }, false},
		{"confirmation wait over an hour", func(s *Settings) { s.StaleInboxConfirmationSeconds = 3601 }, false},
	} {
		t.Run(c.name, func(t *testing.T) {
			settings := DefaultSettings()
			c.change(&settings)
			if err := settings.Validate(); (err == nil) != c.valid {
				t.Errorf("Validate = %v, want valid = %v", err, c.valid)
			}
		})
	}
}
