package main

import (
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/llm_ingress/configpb"
)

const testCodexCatalog = `{"models":[{"slug":"test/route-a"},{"slug":"test/route-b"}]}`

func writeCodexCatalog(t *testing.T, contents string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "codex_models.json")
	if err := os.WriteFile(path, []byte(contents), 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

func TestCodexCatalogIsServedAuthenticatedAndSignalledOnResponses(t *testing.T) {
	tokenA := workloadToken(t, "test-sandbox-a")
	backend := newFakeLiteLLM()
	backendServer := newBackendServer(t, backend)
	ingress := newConfiguredIngressTestServer(t, api.TokenReviews(), &configpb.Config{
		LitellmUrl: backendServer.URL, LitellmKey: testBackendKey,
		CodexModelCatalogFile: writeCodexCatalog(t, testCodexCatalog),
	})
	client := testClient()
	defer client.CloseIdleConnections()

	catalog, catalogBody := request(t, client, http.MethodGet, ingress.URL+codexCatalogPath+"?client_version=0.0.0", "", bearer(tokenA))
	unauthenticated := doRequest(t, client, ingress.URL+codexCatalogPath, http.Header{})
	_ = unauthenticated.Body.Close()
	responses, _ := request(t, client, http.MethodPost, ingress.URL+"/v1/responses", `{}`, bearer(tokenA))
	messages, _ := request(t, client, http.MethodPost, ingress.URL+"/v1/messages", `{}`, bearer(tokenA))

	etag := catalog.Header.Get("ETag")
	if catalog.StatusCode != http.StatusOK || string(catalogBody) != testCodexCatalog || etag == "" ||
		unauthenticated.StatusCode != http.StatusUnauthorized {
		t.Fatalf("catalog=%d %s etag=%q unauthenticated=%d", catalog.StatusCode, catalogBody, etag, unauthenticated.StatusCode)
	}
	if got := responses.Header.Get("X-Models-Etag"); got != etag {
		t.Fatalf("X-Models-Etag on a Responses reply = %q, want the catalog ETag %q", got, etag)
	}
	if got := messages.Header.Get("X-Models-Etag"); got != "" {
		t.Fatalf("X-Models-Etag on a Messages reply = %q", got)
	}
	for _, got := range backend.snapshotRequests() {
		if strings.HasPrefix(got.path, "/agentplane/") {
			t.Fatalf("catalog request reached LiteLLM: %s", got.path)
		}
	}
}

func TestCodexCatalogEndpointWithoutCatalogIsNotForwarded(t *testing.T) {
	tokenA := workloadToken(t, "test-sandbox-a")
	backend := newFakeLiteLLM()
	backendServer := newBackendServer(t, backend)
	ingress := newIngressTestServer(t, api.TokenReviews(), backendServer.URL)
	client := testClient()
	defer client.CloseIdleConnections()

	catalog, _ := request(t, client, http.MethodGet, ingress.URL+codexCatalogPath, "", bearer(tokenA))
	responses, _ := request(t, client, http.MethodPost, ingress.URL+"/v1/responses", `{}`, bearer(tokenA))

	if catalog.StatusCode != http.StatusNotFound || responses.Header.Get("X-Models-Etag") != "" {
		t.Fatalf("catalog=%d X-Models-Etag=%q", catalog.StatusCode, responses.Header.Get("X-Models-Etag"))
	}
	for _, got := range backend.snapshotRequests() {
		if strings.HasPrefix(got.path, "/agentplane/") {
			t.Fatalf("catalog request reached LiteLLM: %s", got.path)
		}
	}
}

func TestCodexCatalogEtagFollowsContent(t *testing.T) {
	first, err := loadCodexCatalog(writeCodexCatalog(t, testCodexCatalog))
	if err != nil {
		t.Fatal(err)
	}
	same, err := loadCodexCatalog(writeCodexCatalog(t, testCodexCatalog))
	if err != nil {
		t.Fatal(err)
	}
	edited, err := loadCodexCatalog(writeCodexCatalog(t, `{"models":[{"slug":"test/route-a"}]}`))
	if err != nil {
		t.Fatal(err)
	}
	if first.etag != same.etag || first.etag == edited.etag {
		t.Fatalf("etags: first=%s same=%s edited=%s", first.etag, same.etag, edited.etag)
	}
}

func TestCodexCatalogRejectsWhatCodexWouldRefuse(t *testing.T) {
	for name, contents := range map[string]string{
		"not JSON":       `not json`,
		"no models":      `{"models":[]}`,
		"missing slug":   `{"models":[{"slug":"test/route-a"},{"display_name":"no slug"}]}`,
		"duplicate slug": `{"models":[{"slug":"test/route-a"},{"slug":"test/route-a"}]}`,
		"over 1 MiB":     `{"models":[{"slug":"test/route-a","base_instructions":"` + strings.Repeat("x", maxCodexCatalogBytes) + `"}]}`,
	} {
		t.Run(name, func(t *testing.T) {
			if _, err := loadCodexCatalog(writeCodexCatalog(t, contents)); err == nil {
				t.Fatal("catalog was accepted")
			}
		})
	}
	if _, err := loadCodexCatalog(filepath.Join(t.TempDir(), "missing.json")); err == nil {
		t.Fatal("a missing file was accepted")
	}
}
