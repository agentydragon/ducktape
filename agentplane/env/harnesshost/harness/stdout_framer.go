package harness

import (
	"bytes"
	"errors"
)

// StdoutBatch contains the complete frames found in one process read.
type StdoutBatch struct {
	Lines [][]byte
	Err   error
	// PendingTooLong means Lines may be consumed before returning Err for the unfinished suffix.
	PendingTooLong bool
}

// StdoutFramer splits process stdout into newline-delimited frames.
type StdoutFramer struct {
	pending []byte
}

// Feed splits one process read into newline-delimited frames while retaining its incomplete suffix.
func (f *StdoutFramer) Feed(chunk []byte) StdoutBatch {
	f.pending = append(f.pending, chunk...)
	var batch StdoutBatch
	for {
		i := bytes.IndexByte(f.pending, '\n')
		if i < 0 {
			break
		}
		if i > MaxFrameLineBytes {
			batch.Err = errStdoutFrameTooLong
			return batch
		}
		batch.Lines = append(batch.Lines, append([]byte(nil), bytes.TrimRight(f.pending[:i], "\r")...))
		f.pending = f.pending[i+1:]
	}
	if len(f.pending) > MaxFrameLineBytes {
		batch.Err = errStdoutFrameTooLong
		batch.PendingTooLong = true
	}
	return batch
}

// Finish returns the final unterminated frame at EOF, if any.
func (f *StdoutFramer) Finish() [][]byte {
	if len(f.pending) == 0 {
		return nil
	}
	line := append([]byte(nil), bytes.TrimRight(f.pending, "\r")...)
	f.pending = nil
	return [][]byte{line}
}

var errStdoutFrameTooLong = errors.New("native frame exceeded the 64 MiB limit")
