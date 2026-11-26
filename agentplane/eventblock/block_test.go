package eventblock

import (
	"bytes"
	"encoding/binary"
	"testing"
)

func TestCut(t *testing.T) {
	p, err := NewPacker(Profile{16, 3, 3})
	if err != nil {
		t.Fatal(err)
	}
	for _, tc := range []struct {
		name  string
		sizes []int
		flush bool
		want  Cut
	}{
		{"equality", []int{12}, false, Cut{Seal, 1}},
		{"crossing", []int{6, 10}, false, Cut{Seal, 2}},
		{"large singleton", []int{128}, false, Cut{Seal, 1}},
		{"count", []int{1, 1, 1}, false, Cut{Seal, 3}},
		{"carry tail", []int{2}, false, Cut{NeedMore, 0}},
		{"flush tail", []int{2}, true, Cut{Seal, 1}},
		{"format barrier", []int{1, MaxBytes}, true, Cut{Seal, 1}},
		{"unrepresentable", []int{MaxBytes}, false, Cut{Standalone, 1}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var f [][]byte
			for _, n := range tc.sizes {
				f = append(f, make([]byte, n))
			}
			if got := p.Next(f, tc.flush); got != tc.want {
				t.Fatalf("cut=%v want=%v", got, tc.want)
			}
		})
	}
}

func TestWire(t *testing.T) {
	p, err := NewPacker(Profile{1024, 2, 3})
	if err != nil {
		t.Fatal(err)
	}
	for _, frames := range [][][]byte{{[]byte("first"), nil, []byte{0xff, 0, 0x80}}, {make([]byte, MaxBytes-4)}} {
		b, err := p.Encode(frames)
		if err != nil {
			t.Fatal(err)
		}
		got, err := Decode(b)
		if err != nil || len(got) != len(frames) {
			t.Fatalf("decode: %v", err)
		}
		for i, f := range got {
			if !bytes.Equal(f, frames[i]) {
				t.Fatal("frame changed")
			}
		}
		if _, err := Decode(b[:len(b)-1]); err == nil {
			t.Fatal("accepted truncation")
		}
		binary.BigEndian.PutUint32(b[len(magic):], 4)
		if _, err := Decode(b); err == nil {
			t.Fatal("accepted inconsistent decoded size")
		}
	}
}
