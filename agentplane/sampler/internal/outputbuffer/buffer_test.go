package outputbuffer

import (
	"bytes"
	"testing"
)

func TestRetainsHeadAndTailAndRestores(t *testing.T) {
	buffer := New(8)
	buffer.Append([]byte("abc"))
	buffer.Append([]byte("defghij"))
	buffer.Append([]byte("klm"))
	if got := string(buffer.RetainedBytes()); got != "abcdjklm" || buffer.OmittedBytes() != 5 || buffer.TotalBytes() != 13 {
		t.Fatalf("retained=%q omitted=%d total=%d", got, buffer.OmittedBytes(), buffer.TotalBytes())
	}
	if got := string(buffer.WithOmissionMarker()); got != "abcd\n... 5 bytes omitted ...\njklm" {
		t.Fatalf("marked output = %q", got)
	}

	restored, err := Restore(8, buffer.RetainedBytes(), buffer.OmittedBytes())
	if err != nil {
		t.Fatal(err)
	}
	restored.Append([]byte("n"))
	buffer.Append([]byte("n"))
	if !bytes.Equal(restored.RetainedBytes(), buffer.RetainedBytes()) || restored.OmittedBytes() != buffer.OmittedBytes() {
		t.Fatalf("restored buffer diverged: %q/%d vs %q/%d", restored.RetainedBytes(), restored.OmittedBytes(), buffer.RetainedBytes(), buffer.OmittedBytes())
	}
	if _, err := Restore(8, []byte("short"), 3); err == nil {
		t.Fatal("accepted malformed retained output")
	}
	if _, err := Restore(8, []byte("123456789"), 0); err == nil {
		t.Fatal("accepted retained output beyond capacity without omissions")
	}
}

func TestRetainsArbitraryBytes(t *testing.T) {
	buffer := New(4)
	buffer.Append([]byte{0x00, 0xff, 0x80, 0x01, 0xfe, 0x02})
	if got, want := buffer.RetainedBytes(), []byte{0x00, 0xff, 0xfe, 0x02}; !bytes.Equal(got, want) {
		t.Fatalf("retained bytes = %v, want %v", got, want)
	}
	if buffer.OmittedBytes() != 2 {
		t.Fatalf("omitted bytes = %d, want 2", buffer.OmittedBytes())
	}
}
