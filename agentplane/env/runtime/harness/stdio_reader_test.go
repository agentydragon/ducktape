package harness

import (
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

func readText(t *testing.T, r io.Reader) []string {
	t.Helper()
	var got []string
	if err := ReadText(r, func(text string) error { got = append(got, text); return nil }); err != nil {
		t.Fatalf("ReadText: %v", err)
	}
	return got
}

func TestReadTextKeepsCharacterSplitAcrossReads(t *testing.T) {
	euro := []byte("€") // three bytes
	for split := 1; split < len(euro); split++ {
		r := &chunkReader{chunks: [][]byte{append([]byte("price "), euro[:split]...), append(euro[split:], " 5"...)}}
		if got := strings.Join(readText(t, r), ""); got != "price € 5" {
			t.Errorf("split at %d: text = %q, want %q", split, got, "price € 5")
		}
	}
}

func TestReadTextReplacesInvalidAndTruncatedBytes(t *testing.T) {
	r := &chunkReader{chunks: [][]byte{{'a', 0xff, 'b'}, {0xe2, 0x82}}}
	if got := strings.Join(readText(t, r), ""); got != "a�b�" {
		t.Fatalf("text = %q, want invalid byte and truncated EOF sequence replaced", got)
	}
}

func TestReadTextOneByteReads(t *testing.T) {
	const text = "žluťoučký kůň 🐎"
	if got := strings.Join(readText(t, iotest.OneByteReader(strings.NewReader(text))), ""); got != text {
		t.Fatalf("text = %q, want %q", got, text)
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
