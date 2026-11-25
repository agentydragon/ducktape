package content

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
)

type (
	ContentID   string
	ContentKind string
)

// ContentText holds reusable request instructions/system prompts, not scalar
// fields extracted from native items or blocks.
const ContentText ContentKind = "text"

// ContentJavaScriptToolCatalog holds the original JSON array bytes from a JavaScript
// execute BoundaryRecord. A replay must preserve even equivalent JSON spellings.
const ContentJavaScriptToolCatalog ContentKind = "javascript_tool_catalog"

// Kinds are defined and validated by the codecs that own their payloads.
func (kind ContentKind) Valid() bool { return kind != "" }

type StoredContent struct {
	ID      ContentID       `json:"id"`
	Kind    ContentKind     `json:"kind"`
	Payload json.RawMessage `json:"payload"`
}

type ContentWriter interface {
	PutContent(ContentKind, json.RawMessage) (ContentID, error)
}

type ContentReader interface {
	GetContent(ContentID) (StoredContent, error)
}

// PrepareContent canonicalizes provider JSON with numeric precision and keeps
// the original bytes for content kinds whose replay contract requires them.
func PrepareContent(kind ContentKind, payload json.RawMessage) (StoredContent, error) {
	if !kind.Valid() {
		return StoredContent{}, fmt.Errorf("unknown content kind %q", kind)
	}
	var stored json.RawMessage
	if kind == ContentJavaScriptToolCatalog {
		if !bytes.HasPrefix(bytes.TrimSpace(payload), []byte("[")) || !json.Valid(payload) {
			return StoredContent{}, errors.New("JavaScript tool catalog must be a JSON array")
		}
		stored = bytes.Clone(payload)
	} else {
		var err error
		stored, err = canonicalJSON(payload)
		if err != nil {
			return StoredContent{}, err
		}
	}
	digest := sha256.New()
	digest.Write([]byte("agentplane/provider-content/v1\x00"))
	digest.Write([]byte(kind))
	digest.Write([]byte{0})
	digest.Write(stored)
	return StoredContent{ID: ContentID(hex.EncodeToString(digest.Sum(nil))), Kind: kind, Payload: stored}, nil
}

func canonicalJSON(payload json.RawMessage) (json.RawMessage, error) {
	decoder := json.NewDecoder(bytes.NewReader(payload))
	decoder.UseNumber()
	var value any
	if err := decoder.Decode(&value); err != nil {
		return nil, err
	}
	if err := decoder.Decode(new(any)); !errors.Is(err, io.EOF) {
		return nil, errors.New("content contains trailing JSON")
	}
	return json.Marshal(value)
}

func VerifyContent(content StoredContent) error {
	expected, err := PrepareContent(content.Kind, content.Payload)
	if err != nil {
		return err
	}
	if expected.ID != content.ID {
		return fmt.Errorf("content hash mismatch for %s", content.ID)
	}
	return nil
}
