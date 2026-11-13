package settings

import "testing"

func TestRetentionBoundsAreIndependentOfEnablingGitHub(t *testing.T) {
	for _, test := range []struct {
		name  string
		days  *int
		valid bool
	}{
		{"infinite", nil, true},
		{"shortest", new(1), true},
		{"longest", new(3650), true},
		{"zero", new(0), false},
		{"negative", new(-1), false},
		{"excessive", new(3651), false},
	} {
		t.Run(test.name, func(t *testing.T) {
			if err := (Retention{Days: test.days}).Validate(); (err == nil) != test.valid {
				t.Fatalf("Validate() = %v, valid = %v", err, test.valid)
			}
		})
	}
	if days := DefaultRetention().Days; days == nil || *days != 7 {
		t.Fatalf("default retention = %v, want seven days", days)
	}
}
