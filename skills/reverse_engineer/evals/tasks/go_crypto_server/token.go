package main

import (
	"encoding/hex"
	"os"
	"sync"
	"time"
)

type tokenSource struct {
	mu    sync.Mutex
	state uint64
}

func newTokenSource() *tokenSource {
	seed := uint64(time.Now().UnixNano()) ^ uint64(os.Getpid())
	return &tokenSource{state: seed}
}

func (t *tokenSource) next() uint64 {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.state += 0x9E3779B97F4A7C15
	z := t.state
	z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9
	z = (z ^ (z >> 27)) * 0x94D049BB133111EB
	z = z ^ (z >> 31)
	return z
}

func (t *tokenSource) issue() string {
	hi := t.next()
	lo := t.next()
	var buf [16]byte
	for i := 0; i < 8; i++ {
		buf[7-i] = byte(hi >> (i * 8))
		buf[15-i] = byte(lo >> (i * 8))
	}
	return hex.EncodeToString(buf[:])
}
