package main

import (
	"errors"
	"sort"
	"sync"
)

type storedNote struct {
	NoteID     string
	Title      string
	Ciphertext []byte
}

type session struct {
	Key   [16]byte
	Notes map[string]storedNote
	Order []string
}

type noteStore struct {
	mu       sync.Mutex
	sessions map[string]*session
	nextID   uint64
}

var (
	errUnknownToken = errors.New("unknown token")
	errUnknownNote  = errors.New("unknown note id")
)

func newNoteStore() *noteStore {
	return &noteStore{sessions: make(map[string]*session)}
}

func (s *noteStore) register(token string, key [16]byte) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.sessions[token] = &session{Key: key, Notes: make(map[string]storedNote)}
}

func (s *noteStore) put(token, title string, ciphertext []byte) (string, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	sess, ok := s.sessions[token]
	if !ok {
		return "", errUnknownToken
	}
	s.nextID++
	id := formatNoteID(s.nextID)
	sess.Notes[id] = storedNote{NoteID: id, Title: title, Ciphertext: ciphertext}
	sess.Order = append(sess.Order, id)
	return id, nil
}

func (s *noteStore) get(token, noteID string) (storedNote, [16]byte, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	sess, ok := s.sessions[token]
	if !ok {
		return storedNote{}, [16]byte{}, errUnknownToken
	}
	n, ok := sess.Notes[noteID]
	if !ok {
		return storedNote{}, [16]byte{}, errUnknownNote
	}
	return n, sess.Key, nil
}

func (s *noteStore) listIDs(token string) ([]string, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	sess, ok := s.sessions[token]
	if !ok {
		return nil, errUnknownToken
	}
	ids := make([]string, len(sess.Order))
	copy(ids, sess.Order)
	sort.Strings(ids)
	return ids, nil
}

func (s *noteStore) snapshot(token string) ([]storedNote, [16]byte, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	sess, ok := s.sessions[token]
	if !ok {
		return nil, [16]byte{}, errUnknownToken
	}
	out := make([]storedNote, 0, len(sess.Order))
	for _, id := range sess.Order {
		out = append(out, sess.Notes[id])
	}
	return out, sess.Key, nil
}

func formatNoteID(n uint64) string {
	const digits = "0123456789ABCDEF"
	var buf [16]byte
	for i := 15; i >= 0; i-- {
		buf[i] = digits[n&0xF]
		n >>= 4
	}
	return "n_" + string(buf[:])
}
