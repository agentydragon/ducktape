package main

import (
	"encoding/hex"
	"encoding/json"
	"errors"
	"net/http"
)

type server struct {
	store  *noteStore
	tokens *tokenSource
}

func newServer() *server {
	return &server{
		store:  newNoteStore(),
		tokens: newTokenSource(),
	}
}

func (s *server) routes() *http.ServeMux {
	mux := http.NewServeMux()
	mux.HandleFunc("/v1/register", s.handleRegister)
	mux.HandleFunc("/v1/note/put", s.handlePut)
	mux.HandleFunc("/v1/note/get", s.handleGet)
	mux.HandleFunc("/v1/note/list", s.handleList)
	mux.HandleFunc("/v1/export", s.handleExport)
	return mux
}

func tokenToKey(token string) ([16]byte, error) {
	var key [16]byte
	raw, err := hex.DecodeString(token)
	if err != nil {
		return key, err
	}
	if len(raw) != 16 {
		return key, errors.New("token must be 16 bytes")
	}
	copy(key[:], raw)
	return key, nil
}

func writeError(w http.ResponseWriter, op string, code int) {
	resp := errorResponse{Version: protocolVersion, Op: op, Code: code, Reason: reasonForCode(code)}
	w.Header().Set("Content-Type", "application/json")
	switch code {
	case codeBadRequest, codeUnknownToken, codeUnknownNote, codeBadCiphertext, codeBadMAC:
		w.WriteHeader(http.StatusBadRequest)
	default:
		w.WriteHeader(http.StatusInternalServerError)
	}
	_ = json.NewEncoder(w).Encode(resp)
}

func writeOK(w http.ResponseWriter, op string, body any) {
	resp := envelope{Version: protocolVersion, Op: op, Body: body}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	_ = json.NewEncoder(w).Encode(resp)
}

func decodeRequest[T any](r *http.Request, out *T) error {
	var env struct {
		Version string          `json:"v"`
		Op      string          `json:"op"`
		Body    json.RawMessage `json:"body"`
	}
	if err := json.NewDecoder(r.Body).Decode(&env); err != nil {
		return err
	}
	if env.Version != protocolVersion {
		return errors.New("protocol version mismatch")
	}
	return json.Unmarshal(env.Body, out)
}

func (s *server) handleRegister(w http.ResponseWriter, r *http.Request) {
	const op = "register"
	var req registerRequest
	if err := decodeRequest(r, &req); err != nil {
		writeError(w, op, codeBadRequest)
		return
	}
	token := s.tokens.issue()
	key, err := tokenToKey(token)
	if err != nil {
		writeError(w, op, codeInternalError)
		return
	}
	s.store.register(token, key)
	writeOK(w, op, registerResponse{Token: token})
}

func (s *server) handlePut(w http.ResponseWriter, r *http.Request) {
	const op = "note.put"
	var req putRequest
	if err := decodeRequest(r, &req); err != nil {
		writeError(w, op, codeBadRequest)
		return
	}
	key, err := tokenToKey(req.Token)
	if err != nil {
		writeError(w, op, codeBadRequest)
		return
	}
	ct := encryptECB([]byte(req.Plaintext), key)
	id, err := s.store.put(req.Token, req.Title, ct)
	if err != nil {
		writeError(w, op, codeUnknownToken)
		return
	}
	writeOK(w, op, putResponse{NoteID: id})
}

func (s *server) handleGet(w http.ResponseWriter, r *http.Request) {
	const op = "note.get"
	var req getRequest
	if err := decodeRequest(r, &req); err != nil {
		writeError(w, op, codeBadRequest)
		return
	}
	note, key, err := s.store.get(req.Token, req.NoteID)
	switch {
	case errors.Is(err, errUnknownToken):
		writeError(w, op, codeUnknownToken)
		return
	case errors.Is(err, errUnknownNote):
		writeError(w, op, codeUnknownNote)
		return
	case err != nil:
		writeError(w, op, codeInternalError)
		return
	}
	plain, err := decryptECB(note.Ciphertext, key)
	if err != nil {
		writeError(w, op, codeBadCiphertext)
		return
	}
	writeOK(w, op, getResponse{Title: note.Title, Plaintext: string(plain)})
}

func (s *server) handleList(w http.ResponseWriter, r *http.Request) {
	const op = "note.list"
	var req listRequest
	if err := decodeRequest(r, &req); err != nil {
		writeError(w, op, codeBadRequest)
		return
	}
	ids, err := s.store.listIDs(req.Token)
	if err != nil {
		writeError(w, op, codeUnknownToken)
		return
	}
	writeOK(w, op, listResponse{NoteIDs: ids})
}

func (s *server) handleExport(w http.ResponseWriter, r *http.Request) {
	const op = "export"
	var req exportRequest
	if err := decodeRequest(r, &req); err != nil {
		writeError(w, op, codeBadRequest)
		return
	}
	notes, key, err := s.store.snapshot(req.Token)
	if err != nil {
		writeError(w, op, codeUnknownToken)
		return
	}
	payload := buildSnapshotBytes(notes)
	tag := macSign(key, payload)
	signed := append([]byte{}, payload...)
	signed = append(signed, tag[:]...)
	writeOK(w, op, exportResponse{Blob: encodeBase32Custom(signed)})
}

func buildSnapshotBytes(notes []storedNote) []byte {
	var out []byte
	for _, n := range notes {
		out = append(out, byte(len(n.NoteID)))
		out = append(out, []byte(n.NoteID)...)
		out = append(out, byte(len(n.Title)))
		out = append(out, []byte(n.Title)...)
		var ctLen [4]byte
		ctLen[0] = byte(len(n.Ciphertext) >> 24)
		ctLen[1] = byte(len(n.Ciphertext) >> 16)
		ctLen[2] = byte(len(n.Ciphertext) >> 8)
		ctLen[3] = byte(len(n.Ciphertext))
		out = append(out, ctLen[:]...)
		out = append(out, n.Ciphertext...)
	}
	return out
}
