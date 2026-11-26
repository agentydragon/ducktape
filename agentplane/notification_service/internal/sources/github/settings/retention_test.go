package settings

import "testing"

func TestRetentionBoundsAreIndependentOfEnablingGitHub(t *testing.T) {
	for _, test := range []struct {
		name        string
		days        *int
		payloadDays *int
		valid       bool
	}{
		{"infinite", nil, nil, true},
		{"shortest facts and zero bodies", new(1), new(0), true},
		{"shorter body retention", new(7), new(3), true},
		{"longest", new(3650), new(3650), true},
		{"zero facts", new(0), new(0), false},
		{"negative facts", new(-1), new(0), false},
		{"excessive facts", new(3651), new(0), false},
		{"negative body retention", new(7), new(-1), false},
		{"excessive body retention", new(3650), new(3651), false},
		{"body retention longer than facts", new(2), new(3), false},
		{"infinite body with finite facts", new(7), nil, false},
		{"finite body with infinite facts", nil, new(3), true},
	} {
		t.Run(test.name, func(t *testing.T) {
			if err := (Retention{Days: test.days, PayloadDays: test.payloadDays}).Validate(); (err == nil) != test.valid {
				t.Fatalf("Validate() = %v, valid = %v", err, test.valid)
			}
		})
	}
	if days := DefaultRetention().Days; days == nil || *days != 7 {
		t.Fatalf("default retention = %v, want seven days", days)
	}
	if days := DefaultRetention().PayloadDays; days == nil || *days != 3 {
		t.Fatalf("default payload retention = %v, want three days", days)
	}
}
