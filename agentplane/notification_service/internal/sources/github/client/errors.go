package client

import (
	"errors"
	"fmt"
)

// RateLimitedError is a GitHub rate limit. The caller persists RetrySeconds as backoff instead of sleeping.
type RateLimitedError struct {
	StatusCode   int
	RetrySeconds int
}

func (e *RateLimitedError) Error() string {
	return fmt.Sprintf("GitHub rate limited (HTTP %d); retry in %ds", e.StatusCode, e.RetrySeconds)
}

// AccessError is a confirmed loss of access: GitHub denied the App's credentials or permissions, or no longer shows
// the resource to its installation. StatusCode is the status of the response that showed it.
type AccessError struct {
	StatusCode int
}

func (e *AccessError) Error() string {
	return fmt.Sprintf("GitHub App access unavailable (HTTP %d); check credentials and permissions", e.StatusCode)
}

// NotInstalledError is the App's installation lookup returning 404 for a repository. It is an AccessError for
// errors.As, and a caller may tolerate it where the repository is optional, such as a pull request's fork.
type NotInstalledError struct {
	AccessError
}

func (e *NotInstalledError) Error() string {
	return "GitHub App has no accessible installation for this repository"
}

func (e *NotInstalledError) Unwrap() error { return &e.AccessError }

// SuspendedError is an installation GitHub reports as suspended. It is an AccessError for errors.As, but never a
// NotInstalledError.
type SuspendedError struct {
	AccessError
}

func (e *SuspendedError) Error() string { return "GitHub App installation is suspended" }

func (e *SuspendedError) Unwrap() error { return &e.AccessError }

// SourceChangedError is a repository, installation or commit identity that no longer matches what was authorized.
type SourceChangedError struct {
	Reason string
}

func (e *SourceChangedError) Error() string { return e.Reason }

// StatusError is a response the client has no more specific error for, such as a 5xx or a redirect.
type StatusError struct {
	StatusCode int
}

func (e *StatusError) Error() string {
	return fmt.Sprintf("GitHub responded with HTTP %d", e.StatusCode)
}

// TransportError is a request that got no response: a connection failure or the request timeout.
type TransportError struct {
	Err error
}

func (e *TransportError) Error() string { return fmt.Sprintf("GitHub request failed: %v", e.Err) }

func (e *TransportError) Unwrap() error { return e.Err }

// InvalidResponseError is a successful response whose body lacks, or mangles, a field the client needs.
type InvalidResponseError struct {
	Reason string
}

func (e *InvalidResponseError) Error() string {
	return "GitHub returned an invalid response: " + e.Reason
}

// errMissing is a tolerated 404; the calls that allow one turn it into their own answer.
var errMissing = errors.New("GitHub resource not found")
