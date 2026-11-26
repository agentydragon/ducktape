package harness

import (
	"bytes"
	"errors"
	"io"
	"unicode/utf8"
)

const (
	stdoutReadBytes = 64 * 1024
	textReadBytes   = 4096
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

// ReadText passes r's output to sink as valid UTF-8, one call per read. A multibyte character split across
// reads is held until it completes; invalid bytes become U+FFFD. It returns the first read or sink error,
// or nil at EOF.
func ReadText(r io.Reader, sink func(string) error) error {
	buffer := make([]byte, textReadBytes)
	held := 0
	for {
		n, err := r.Read(buffer[held:])
		filled := held + n
		emit := filled
		if err == nil {
			emit -= incompleteRuneSuffix(buffer[:filled])
		}
		if emit > 0 {
			if e := sink(string(bytes.ToValidUTF8(buffer[:emit], []byte("�")))); e != nil {
				return e
			}
		}
		held = copy(buffer, buffer[emit:filled])
		if errors.Is(err, io.EOF) {
			return nil
		}
		if err != nil {
			return err
		}
	}
}

// incompleteRuneSuffix returns the length of a truncated UTF-8 sequence ending b, or 0.
func incompleteRuneSuffix(b []byte) int {
	for i := len(b) - 1; i >= 0 && i > len(b)-utf8.UTFMax; i-- {
		if utf8.RuneStart(b[i]) {
			if utf8.FullRune(b[i:]) {
				return 0
			}
			return len(b) - i
		}
	}
	return 0
}
