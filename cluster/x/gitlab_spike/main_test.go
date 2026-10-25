package main

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestHealthz(t *testing.T) {
	request := httptest.NewRequest(http.MethodGet, "/healthz", nil)
	response := httptest.NewRecorder()

	handler(response, request)

	if response.Code != http.StatusOK {
		t.Fatalf("health status = %d, want %d", response.Code, http.StatusOK)
	}
}

func TestRootReportsSpikeImage(t *testing.T) {
	request := httptest.NewRequest(http.MethodGet, "/", nil)
	response := httptest.NewRecorder()

	handler(response, request)

	if response.Code != http.StatusOK {
		t.Fatalf("root status = %d, want %d", response.Code, http.StatusOK)
	}
	if response.Body.String() != "gitlab spike image is running\n" {
		t.Fatalf("root body = %q", response.Body.String())
	}
}
