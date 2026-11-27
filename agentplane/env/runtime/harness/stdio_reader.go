package harness

import (
	"bytes"
	"errors"
	"io"
)

const (
	stdoutReadBytes = 64 * 1024
	chunkReadBytes  = 4096
)

// ReadFramedStdout passes the complete frames of each read of r to deliver, then the final unterminated frame
// at EOF. It returns the first read, framing or deliver error, or nil at EOF. A frame longer than
// MaxFrameLineBytes fails the read; complete frames before an oversized unterminated suffix are delivered first.
func ReadFramedStdout(r io.Reader, deliver func([][]byte) error) error {
	buffer := make([]byte, stdoutReadBytes)
	var framer StdoutFramer
	for {
		n, err := r.Read(buffer)
		if n > 0 {
			batch := framer.Feed(buffer[:n])
			if batch.Err != nil && !batch.PendingTooLong {
				return batch.Err
			}
			if len(batch.Lines) > 0 {
				if e := deliver(batch.Lines); e != nil {
					return e
				}
			}
			if batch.Err != nil {
				return batch.Err
			}
		}
		if errors.Is(err, io.EOF) {
			if lines := framer.Finish(); len(lines) > 0 {
				return deliver(lines)
			}
			return nil
		}
		if err != nil {
			return err
		}
	}
}

// ReadChunks passes each read of r to deliver unchanged, in a buffer deliver may keep. It returns the first
// read or deliver error, or nil at EOF.
func ReadChunks(r io.Reader, deliver func([]byte) error) error {
	buffer := make([]byte, chunkReadBytes)
	for {
		n, err := r.Read(buffer)
		if n > 0 {
			if e := deliver(bytes.Clone(buffer[:n])); e != nil {
				return e
			}
		}
		if errors.Is(err, io.EOF) {
			return nil
		}
		if err != nil {
			return err
		}
	}
}
