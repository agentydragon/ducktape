package inbox

import (
	"errors"
	"testing"

	"github.com/lib/pq"
)

// requireConstraint requires err to be the database refusing a row for the named constraint, or for any constraint
// of the code.
func requireConstraint(t *testing.T, err error, code pq.ErrorCode, constraint string) {
	t.Helper()
	var refused *pq.Error
	if !errors.As(err, &refused) || refused.Code != code || (constraint != "" && refused.Constraint != constraint) {
		t.Fatalf("error = %v, want %s %q", err, code.Name(), constraint)
	}
}
