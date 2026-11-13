// Package httperror logs the cause of a server-side failure that an API response hides.
package httperror

import (
	"errors"
	"log/slog"
	"net/http"

	"github.com/danielgtaylor/huma/v2"
)

// LogServerError logs cause when mapped is a 5xx response and returns mapped. Error mappers turn an
// unclassified upstream failure into a generic 503 or 504 without its cause, so this log is the only
// record of which dependency failed and how.
func LogServerError(mapped, cause error) error {
	var status huma.StatusError
	if errors.As(mapped, &status) && status.GetStatus() >= http.StatusInternalServerError {
		slog.Error("upstream request failed", "status", status.GetStatus(), "response", mapped.Error(), "error", cause)
	}
	return mapped
}
