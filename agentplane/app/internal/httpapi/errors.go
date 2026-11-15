package httpapi

import (
	"encoding/json"
	"net/http"
)

// WriteDetailError emits the app's raw-route error shape.
func WriteDetailError(w http.ResponseWriter, status int, message string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(map[string]string{"detail": message})
}
