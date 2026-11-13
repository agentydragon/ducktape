package centralsampler

import (
	"context"

	"github.com/agentydragon/ducktape/agentplane/central_sampler/historyexport"
)

func (s *Store) ExportSnapshot(ctx context.Context, sessionID string) (historyexport.Snapshot, error) {
	session, err := s.GetSession(ctx, sessionID, "")
	if err != nil {
		return historyexport.Snapshot{}, err
	}
	description, err := Describe(session)
	if err != nil {
		return historyexport.Snapshot{}, err
	}
	return historyexport.Snapshot{Description: description, Cursor: session.Cursor}, nil
}

func (s *Store) CurrentCursor(ctx context.Context, sessionID string) (uint64, error) {
	session, err := s.GetSession(ctx, sessionID, "")
	if err != nil {
		return 0, err
	}
	return session.Cursor, nil
}
