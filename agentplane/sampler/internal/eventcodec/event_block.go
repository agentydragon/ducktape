package eventcodec

import (
	"bytes"
	"compress/gzip"
	"encoding/binary"
	"errors"
	"io"
)

// These bounds are part of the stored block format. See migration 0021.
const (
	MaxBlockEntries  = 32
	MaxBlockBytes    = 2 * 1024 * 1024
	FrameHeaderBytes = 4
)

// PackBlock compresses a sequence of original wire frames as one gzip stream.
// The caller bounds the framed byte size and number of entries when building the block.
func PackBlock(frames [][]byte) ([]byte, error) {
	var compressed bytes.Buffer
	writer, err := gzip.NewWriterLevel(&compressed, gzip.BestSpeed)
	if err != nil {
		return nil, err
	}
	var header [FrameHeaderBytes]byte
	for _, frame := range frames {
		binary.BigEndian.PutUint32(header[:], uint32(len(frame)))
		if _, err := writer.Write(header[:]); err != nil {
			return nil, err
		}
		if _, err := writer.Write(frame); err != nil {
			return nil, err
		}
	}
	if err := writer.Close(); err != nil {
		return nil, err
	}
	return compressed.Bytes(), nil
}

func UnpackBlock(payload []byte, count int) ([][]byte, error) {
	if count < 2 || count > MaxBlockEntries {
		return nil, errors.New("invalid event block cursor span")
	}
	reader, err := gzip.NewReader(bytes.NewReader(payload))
	if err != nil {
		return nil, err
	}
	defer reader.Close()
	raw, err := io.ReadAll(io.LimitReader(reader, MaxBlockBytes+1))
	if err != nil {
		return nil, err
	}
	if len(raw) > MaxBlockBytes {
		return nil, errors.New("event block exceeds decoded size limit")
	}
	frames := make([][]byte, 0, count)
	for range count {
		if len(raw) < FrameHeaderBytes {
			return nil, io.ErrUnexpectedEOF
		}
		length := int(binary.BigEndian.Uint32(raw[:FrameHeaderBytes]))
		raw = raw[FrameHeaderBytes:]
		if length <= 0 || length > len(raw) {
			return nil, errors.New("invalid event block frame length")
		}
		frames = append(frames, raw[:length:length])
		raw = raw[length:]
	}
	if len(raw) != 0 {
		return nil, errors.New("trailing bytes in event block")
	}
	return frames, nil
}
