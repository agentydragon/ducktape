package model

import (
	"fmt"
	"time"
)

// SourceFailureKind is why a source could not be processed.
type SourceFailureKind string

const (
	FailureRateLimited     SourceFailureKind = "rate_limited"
	FailureUnavailable     SourceFailureKind = "unavailable"
	FailureAccessDenied    SourceFailureKind = "access_denied"
	FailureSourceChanged   SourceFailureKind = "source_changed"
	FailureProcessingError SourceFailureKind = "processing_error"
)

func ParseSourceFailureKind(kind string) (SourceFailureKind, error) {
	switch parsed := SourceFailureKind(kind); parsed {
	case FailureRateLimited, FailureUnavailable, FailureAccessDenied, FailureSourceChanged, FailureProcessingError:
		return parsed, nil
	default:
		return "", fmt.Errorf("unknown source failure kind %q", kind)
	}
}

// SourceFailure is the current failure of a subscription's processing.
type SourceFailure struct {
	Kind SourceFailureKind
	// Message is a safe description, never an upstream body or a credential.
	Message string
	// Since is when the failure of this kind began.
	Since      time.Time
	ObservedAt time.Time
}
