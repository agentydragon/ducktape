package sessionstore

import (
	"context"
	"database/sql"
	"encoding/json"

	"github.com/agentydragon/ducktape/agentplane/sampler/internal/content"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/providerrequest"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/sessionstore/internal/contentdb"
)

// newContentDB binds context and provider reference traversal to each transaction.
func newContentDB(ctx context.Context, tx *sql.Tx) *contentdb.Store {
	return contentdb.New(ctx, tx, samplerContentReferences)
}

func samplerContentReferences(kind content.ContentKind, payload json.RawMessage) ([]content.ContentID, error) {
	if kind != contextRecordKind {
		return providerrequest.ContentReferences(kind, payload)
	}
	var record storedContextRecord
	if err := json.Unmarshal(payload, &record); err != nil {
		return nil, err
	}
	if err := record.validate(); err != nil {
		return nil, err
	}
	references := []content.ContentID{record.NativeID}
	if record.Origin.ObjectID != "" {
		references = append(references, content.ContentID(record.Origin.ObjectID))
	}
	return references, nil
}
