// Package httpresponse writes JSON responses for the App's raw HTTP handlers.
package httpresponse

import (
	"encoding/json"
	"net/http"
)

// WriteJSON writes the given status and JSON body.
func WriteJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}

// WriteDetailError writes the App's raw HTTP error body.
func WriteDetailError(w http.ResponseWriter, status int, detail string) {
	WriteJSON(w, status, map[string]string{"detail": detail})
}
