package harness

import (
	"bytes"
	"testing"
)

func TestStdoutFramerFragmentsCRLFAndEOF(t *testing.T) {
	var framer StdoutFramer
	if batch := framer.Feed([]byte("first\r")); len(batch.Lines) != 0 || batch.Err != nil {
		t.Fatalf("first fragment = %+v", batch)
	}
	batch := framer.Feed([]byte("\nsecond\npartial\r"))
	if batch.Err != nil || len(batch.Lines) != 2 || !bytes.Equal(batch.Lines[0], []byte("first")) || !bytes.Equal(batch.Lines[1], []byte("second")) {
		t.Fatalf("complete lines = %q, err = %v", batch.Lines, batch.Err)
	}
	if got := framer.Finish(); len(got) != 1 || string(got[0]) != "partial" {
		t.Fatalf("EOF lines = %q, want [partial]", got)
	}
	if got := framer.Finish(); len(got) != 0 {
		t.Fatalf("second EOF lines = %q, want none", got)
	}
}

func TestStdoutFramerAcceptsLimitAndRejectsOversizedFrames(t *testing.T) {
	const prefix = "before-overflow\n"
	line := make([]byte, MaxFrameLineBytes+len(prefix)+1)
	for i := range line {
		line[i] = 'x'
	}

	var framer StdoutFramer
	{
		if batch := framer.Feed(line[:MaxFrameLineBytes]); len(batch.Lines) != 0 || batch.Err != nil {
			t.Fatalf("maximum fragment = %d lines, err = %v", len(batch.Lines), batch.Err)
		}
		batch := framer.Feed([]byte("\n"))
		if batch.Err != nil || len(batch.Lines) != 1 || len(batch.Lines[0]) != MaxFrameLineBytes {
			t.Fatalf("maximum frame = %d lines, err = %v", len(batch.Lines), batch.Err)
		}
	}
	framer = StdoutFramer{}

	copy(line, prefix)
	batch := framer.Feed(line)
	if batch.Err != errStdoutFrameTooLong || !batch.PendingTooLong {
		t.Fatalf("prefix and oversized unfinished frame = %+v, want prefix plus a pending-line error", batch)
	}
	if len(batch.Lines) != 1 || string(batch.Lines[0]) != "before-overflow" {
		t.Fatalf("prefix lines = %q, want [before-overflow]", batch.Lines)
	}
	batch = framer.Feed([]byte("\n"))
	if batch.Err == nil || batch.PendingTooLong {
		t.Fatalf("oversized terminated frame = %+v, want a complete-line error", batch)
	}
}
