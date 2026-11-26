// Package eventblock stores a bounded, ordered group of opaque journal frames.
package eventblock

import (
	"bytes"
	"encoding/binary"
	"errors"
	"fmt"
	"sync"

	"github.com/klauspost/compress/zstd"
)

// Format v1: APB, version byte, decoded length (big-endian uint32), then
// dictionary-free zstd over a sequence of uint32-length-prefixed frames.
// Reader limits are format constants, independent of the current writer profile.
const (
	magic      = "APB\x01"
	header     = len(magic) + 4
	MaxBytes   = 8 << 20
	MaxEntries = 256
)

type Profile struct {
	TargetBytes   int
	TargetEntries int
	Level         int // stock zstd levels 1 through 4
}

type Packer struct {
	profile Profile
	encoder *zstd.Encoder
}

func NewPacker(p Profile) (*Packer, error) {
	if p.TargetBytes < 1 || p.TargetBytes > MaxBytes || p.TargetEntries < 1 || p.TargetEntries > MaxEntries || p.Level < 1 || p.Level > 4 {
		return nil, fmt.Errorf("invalid event block profile: %+v", p)
	}
	w, err := zstd.NewWriter(nil, zstd.WithEncoderLevel(zstd.EncoderLevelFromZstd(p.Level)), zstd.WithEncoderConcurrency(1))
	if err != nil {
		return nil, err
	}
	return &Packer{profile: p, encoder: w}, nil
}

type Decision int

const (
	NeedMore Decision = iota
	Seal
	Standalone
)

type Cut struct {
	Decision Decision
	N        int
}

// Next plans one block within a single ordered stream. At a database page
// boundary the caller retains a NeedMore tail; at a stream boundary it flushes.
func (p *Packer) Next(frames [][]byte, flushTail bool) Cut {
	return p.next(len(frames), func(i int) int { return len(frames[i]) }, flushTail)
}

// NextSizes applies the same policy to lengths before fetching large payloads.
func (p *Packer) NextSizes(sizes []int, flushTail bool) Cut {
	return p.next(len(sizes), func(i int) int { return sizes[i] }, flushTail)
}

func (p *Packer) next(count int, length func(int) int, flushTail bool) Cut {
	size := 0
	for i := range count {
		n := length(i)
		if n < 0 || n > MaxBytes-size-4 {
			if i == 0 {
				return Cut{Standalone, 1}
			}
			return Cut{Seal, i}
		}
		size += 4 + n
		if size >= p.profile.TargetBytes || i+1 >= p.profile.TargetEntries {
			return Cut{Seal, i + 1}
		}
	}
	if flushTail && count > 0 {
		return Cut{Seal, count}
	}
	return Cut{NeedMore, 0}
}

func (p *Packer) Encode(frames [][]byte) ([]byte, error) {
	if len(frames) == 0 || len(frames) > MaxEntries {
		return nil, errors.New("invalid event block entry count")
	}
	size := 0
	for _, f := range frames {
		if len(f) > MaxBytes-size-4 {
			return nil, errors.New("event block exceeds format limit")
		}
		size += len(f) + 4
	}
	raw := make([]byte, 0, size)
	for _, f := range frames {
		raw = binary.BigEndian.AppendUint32(raw, uint32(len(f)))
		raw = append(raw, f...)
	}
	dst := binary.BigEndian.AppendUint32([]byte(magic), uint32(size))
	return p.encoder.EncodeAll(raw, dst), nil
}

// IsBlock distinguishes the versioned format family from legacy gzip.
func IsBlock(b []byte) bool { return bytes.HasPrefix(b, []byte("APB")) }

var decoder = sync.OnceValues(func() (*zstd.Decoder, error) {
	return zstd.NewReader(nil, zstd.WithDecoderConcurrency(1), zstd.WithDecoderMaxMemory(MaxBytes), zstd.WithDecodeAllCapLimit(true))
})

// Decode returns views into one decoded allocation. Callers treat the views as immutable.
func Decode(b []byte) ([][]byte, error) {
	if len(b) < header || string(b[:len(magic)]) != magic || len(b) > MaxBytes+(64<<10) {
		return nil, errors.New("invalid event block header")
	}
	size := binary.BigEndian.Uint32(b[len(magic):header])
	if size < 4 || size > MaxBytes {
		return nil, errors.New("invalid decoded size")
	}
	d, err := decoder()
	if err != nil {
		return nil, err
	}
	raw, err := d.DecodeAll(b[header:], make([]byte, 0, size))
	if err != nil {
		return nil, err
	}
	if len(raw) != int(size) {
		return nil, errors.New("event block size mismatch")
	}
	var frames [][]byte
	for len(raw) > 0 {
		if len(raw) < 4 || len(frames) >= MaxEntries {
			return nil, errors.New("invalid event block framing")
		}
		n := binary.BigEndian.Uint32(raw)
		raw = raw[4:]
		if uint64(n) > uint64(len(raw)) {
			return nil, errors.New("truncated event frame")
		}
		frames = append(frames, raw[:n:n])
		raw = raw[n:]
	}
	return frames, nil
}
