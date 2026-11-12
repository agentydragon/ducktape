package centralsampler

import (
	"bytes"
	"compress/gzip"
	"io"

	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	"google.golang.org/protobuf/proto"
)

// Historical journal bytes also exist in History, whose duplicate checks compare
// exact records. Compress their physical representation without rewriting events.
// A gzip header cannot begin a valid protobuf message (wire type 7 is invalid).
func encodeStoredEvent(wire []byte) ([]byte, error) {
	if len(wire) < 4096 {
		return wire, nil
	}
	var compressed bytes.Buffer
	writer, err := gzip.NewWriterLevel(&compressed, gzip.BestSpeed)
	if err != nil {
		return nil, err
	}
	if _, err := writer.Write(wire); err != nil {
		return nil, err
	}
	if err := writer.Close(); err != nil {
		return nil, err
	}
	if compressed.Len() >= len(wire) {
		return wire, nil
	}
	return compressed.Bytes(), nil
}

func decodeStoredEvent(wire []byte, entry *eventlogpb.EventEntry) error {
	if len(wire) >= 2 && wire[0] == 0x1f && wire[1] == 0x8b {
		reader, err := gzip.NewReader(bytes.NewReader(wire))
		if err != nil {
			return err
		}
		decoded, err := io.ReadAll(reader)
		closeErr := reader.Close()
		if err != nil {
			return err
		}
		if closeErr != nil {
			return closeErr
		}
		wire = decoded
	}
	return proto.Unmarshal(wire, entry)
}
