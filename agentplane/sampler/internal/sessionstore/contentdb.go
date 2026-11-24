package sessionstore

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/sampler/internal/content"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/providerrequest"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/sessionstore/internal/contentdb"
	"github.com/agentydragon/ducktape/agentplane/sampler/protocol"
)

// newContentDB binds origin and provider reference traversal to each transaction.
func newContentDB(ctx context.Context, tx *sql.Tx) *contentdb.Store {
	return contentdb.New(ctx, tx, samplerContentReferences)
}

func samplerContentReferences(kind content.ContentKind, payload json.RawMessage) ([]content.ContentID, error) {
	if kind != originContentKind {
		return providerrequest.ContentReferences(kind, payload)
	}
	var origin storedOrigin
	if err := json.Unmarshal(payload, &origin); err != nil {
		return nil, err
	}
	if origin.NativeID == "" || (origin.Origin.ContentSource && origin.Origin.SourceID == "") {
		return nil, fmt.Errorf("origin is missing a content reference: %w", protocol.ErrInvalid)
	}
	references := []content.ContentID{origin.NativeID}
	if origin.Origin.ContentSource {
		references = append(references, content.ContentID(origin.Origin.SourceID))
	}
	return references, nil
}
