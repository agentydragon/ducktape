package harness

import (
	"bytes"
	"errors"
	"io"
	"slices"
	"strings"
	"testing"
	"testing/iotest"
)

// chunkReader returns one chunk per Read, so tests choose the read boundaries.
type chunkReader struct{ chunks [][]byte }

func (r *chunkReader) Read(p []byte) (int, error) {
	if len(r.chunks) == 0 {
		return 0, io.EOF
	}
	n := copy(p, r.chunks[0])
	if r.chunks[0] = r.chunks[0][n:]; len(r.chunks[0]) == 0 {
		r.chunks = r.chunks[1:]
	}
	return n, nil
}

func TestReadChunksPassesEachReadUnchanged(t *testing.T) {
	chunks := [][]byte{{'a', 0xe2, 0x82}, {0xac, 0xff, 'b'}}
	var got [][]byte
	if err := ReadChunks(&chunkReader{chunks: slices.Clone(chunks)}, func(chunk []byte) error { got = append(got, chunk); return nil }); err != nil {
		t.Fatalf("ReadChunks: %v", err)
	}
	if !slices.EqualFunc(got, chunks, bytes.Equal) {
		t.Fatalf("chunks = %q, want %q", got, chunks)
	}
}

func TestReadFramedStdoutDeliversPerReadAndFinalFrame(t *testing.T) {
	r := &chunkReader{chunks: [][]byte{[]byte("one\ntw"), []byte("o\r\nthree\nfour")}}
	var got [][]string
	err := ReadFramedStdout(r, func(lines [][]byte) error {
		var batch []string
		for _, line := range lines {
			batch = append(batch, string(line))
		}
		got = append(got, batch)
		return nil
	})
	if err != nil {
		t.Fatalf("ReadFramedStdout: %v", err)
	}
	want := [][]string{{"one"}, {"two", "three"}, {"four"}}
	if !slices.EqualFunc(got, want, slices.Equal) {
		t.Fatalf("batches = %q, want %q", got, want)
	}
}

func TestReadFramedStdoutReturnsReadAndDeliverErrors(t *testing.T) {
	failure := errors.New("read failed")
	if err := ReadFramedStdout(iotest.ErrReader(failure), func([][]byte) error { return nil }); !errors.Is(err, failure) {
		t.Fatalf("read error = %v, want %v", err, failure)
	}
	rejected := errors.New("deliver failed")
	err := ReadFramedStdout(strings.NewReader("one\ntwo\n"), func([][]byte) error { return rejected })
	if !errors.Is(err, rejected) {
		t.Fatalf("deliver error = %v, want %v", err, rejected)
	}
}
