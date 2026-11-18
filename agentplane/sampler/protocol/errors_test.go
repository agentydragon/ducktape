package protocol

import (
	"errors"
	"testing"
	"time"
)

func TestProviderErrorsPreserveTheirCauses(t *testing.T) {
	cause := errors.New("provider operation failed")
	errorsWithCause := []error{
		&ProviderTransientError{Cause: cause},
		&ProviderPhaseTimeoutError{Phase: ProviderTimeoutSemanticIdle, Timeout: time.Second, Cause: cause},
		&ProviderProgressError{Cause: cause},
		&ProviderEvidencePersistenceError{Cause: cause},
	}
	for _, err := range errorsWithCause {
		if !errors.Is(err, cause) {
			t.Errorf("%T does not preserve its cause: %v", err, err)
		}
	}
}
