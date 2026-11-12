package storage

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

// CanonicalContent uses the standard JSON codec; numeric tokens retain their precision.
func CanonicalContent(kind ContentKind, payload json.RawMessage) (StoredContent, error) {
	if !kind.Valid() {
		return StoredContent{}, fmt.Errorf("unknown content kind %q", kind)
	}
	decoder := json.NewDecoder(bytes.NewReader(payload))
	decoder.UseNumber()
	var value any
	err := decoder.Decode(&value)
	if err != nil {
		return StoredContent{}, err
	}
	if err := decoder.Decode(new(any)); !errors.Is(err, io.EOF) {
		return StoredContent{}, fmt.Errorf("content contains trailing JSON")
	}
	canonical, err := json.Marshal(value)
	if err != nil {
		return StoredContent{}, err
	}
	digest := sha256.New()
	digest.Write([]byte("agentplane/provider-content/v1\x00"))
	digest.Write([]byte(kind))
	digest.Write([]byte{0})
	digest.Write(canonical)
	return StoredContent{ID: ContentID(hex.EncodeToString(digest.Sum(nil))), Kind: kind, Payload: canonical}, nil
}

func VerifyContent(content StoredContent) error {
	expected, err := CanonicalContent(content.Kind, content.Payload)
	if err != nil {
		return err
	}
	if expected.ID != content.ID {
		return fmt.Errorf("content hash mismatch for %s", content.ID)
	}
	return nil
}
