package settings

import "testing"

func TestRetentionBoundsAreIndependentOfEnablingGitHub(t *testing.T) {
	for _, test := range []struct {
		name         string
		hours        *int
		payloadHours *int
		valid        bool
	}{
		{"infinite", nil, nil, true},
		{"shortest facts and zero bodies", new(1), new(0), true},
		{"shorter body retention", new(7), new(3), true},
		{"longest", new(87600), new(87600), true},
		{"zero facts", new(0), new(0), false},
		{"negative facts", new(-1), new(0), false},
		{"excessive facts", new(87601), new(0), false},
		{"negative body retention", new(7), new(-1), false},
		{"excessive body retention", new(87600), new(87601), false},
		{"body retention longer than facts", new(2), new(3), false},
		{"infinite body with finite facts", new(7), nil, false},
		{"finite body with infinite facts", nil, new(3), true},
	} {
		t.Run(test.name, func(t *testing.T) {
			if err := (Retention{Hours: test.hours, PayloadHours: test.payloadHours}).Validate(); (err == nil) != test.valid {
				t.Fatalf("Validate() = %v, valid = %v", err, test.valid)
			}
		})
	}
	if hours := DefaultRetention().Hours; hours == nil || *hours != 168 {
		t.Fatalf("default retention = %v, want seven days", hours)
	}
	if hours := DefaultRetention().PayloadHours; hours == nil || *hours != 72 {
		t.Fatalf("default payload retention = %v, want three days", hours)
	}
}
