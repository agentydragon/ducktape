package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
)

const (
	codexCatalogPath = "/agentplane/codex/models"
	// Codex refuses a catalog response larger than this.
	maxCodexCatalogBytes = 1 << 20
)

// codexCatalog is the Codex model catalog, held in memory so that serving it never depends on LiteLLM.
// Its etag is both the catalog response's ETag and the X-Models-Etag header on every Responses reply:
// Codex refetches the catalog when that header differs from the ETag it holds.
type codexCatalog struct {
	body []byte
	etag string
}

func loadCodexCatalog(path string) (*codexCatalog, error) {
	body, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("could not read the Codex model catalog: %w", err)
	}
	if len(body) > maxCodexCatalogBytes {
		return nil, fmt.Errorf("the Codex model catalog is %d bytes, over the %d Codex accepts", len(body), maxCodexCatalogBytes)
	}
	var parsed struct {
		Models []struct {
			Slug string `json:"slug"`
		} `json:"models"`
	}
	if err := json.Unmarshal(body, &parsed); err != nil {
		return nil, fmt.Errorf("the Codex model catalog is not valid JSON of the form {\"models\": [...]}: %w", err)
	}
	if len(parsed.Models) == 0 {
		return nil, errors.New("the Codex model catalog has no models")
	}
	seen := make(map[string]struct{}, len(parsed.Models))
	for _, model := range parsed.Models {
		if model.Slug == "" {
			return nil, errors.New("the Codex model catalog has an entry without a slug")
		}
		if _, duplicate := seen[model.Slug]; duplicate {
			return nil, fmt.Errorf("the Codex model catalog repeats the slug %q", model.Slug)
		}
		seen[model.Slug] = struct{}{}
	}
	sum := sha256.Sum256(body)
	return &codexCatalog{body: body, etag: `"` + hex.EncodeToString(sum[:8]) + `"`}, nil
}
