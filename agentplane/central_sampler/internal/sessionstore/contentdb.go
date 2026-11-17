package sessionstore

import (
	"context"
	"database/sql"

	"github.com/agentydragon/ducktape/agentplane/central_sampler/internal/providerrequest"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/internal/sessionstore/internal/contentdb"
)

// newContentDB binds transaction-scoped persistence to the provider codecs'
// reference traversal without giving contentdb provider-format dependencies.
func newContentDB(ctx context.Context, tx *sql.Tx) *contentdb.Store {
	return contentdb.New(ctx, tx, providerrequest.ContentReferences)
}
