package eventcodec

import (
	"bytes"
	"compress/gzip"
	"io"
)

// Historical journal bytes also exist in History, whose duplicate checks compare
// exact records. Compress their physical representation without rewriting events.
// A gzip header cannot begin a valid protobuf message (wire type 7 is invalid).
func Encode(wire []byte) ([]byte, error) {
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

func Uncompress(wire []byte) ([]byte, error) {
	return unpack(wire, 0)
}

// UnpackBlockEntry unwraps a standalone row without changing its frame bytes.
// An entry too large to fit in a block returns a nil frame.
func UnpackBlockEntry(wire []byte) ([]byte, error) {
	return unpack(wire, MaxBlockBytes-FrameHeaderBytes)
}

func unpack(wire []byte, limit int) ([]byte, error) {
	if len(wire) >= 2 && wire[0] == 0x1f && wire[1] == 0x8b {
		reader, err := gzip.NewReader(bytes.NewReader(wire))
		if err != nil {
			return nil, err
		}
		defer reader.Close()
		var input io.Reader = reader
		if limit > 0 {
			input = io.LimitReader(input, int64(limit)+1)
		}
		decoded, err := io.ReadAll(input)
		if err != nil {
			return nil, err
		}
		wire = decoded
	}
	if limit > 0 && len(wire) > limit {
		return nil, nil
	}
	return wire, nil
}
