package main

const protocolVersion = "ncs/1"

type envelope struct {
	Version string `json:"v"`
	Op      string `json:"op"`
	Body    any    `json:"body,omitempty"`
}

type errorResponse struct {
	Version string `json:"v"`
	Op      string `json:"op"`
	Code    int    `json:"code"`
	Reason  string `json:"reason"`
}

type registerRequest struct {
	ClientHint string `json:"hint,omitempty"`
}

type registerResponse struct {
	Token string `json:"token"`
}

type putRequest struct {
	Token     string `json:"token"`
	Title     string `json:"title"`
	Plaintext string `json:"plaintext"`
}

type putResponse struct {
	NoteID string `json:"note_id"`
}

type getRequest struct {
	Token  string `json:"token"`
	NoteID string `json:"note_id"`
}

type getResponse struct {
	Title     string `json:"title"`
	Plaintext string `json:"plaintext"`
}

type listRequest struct {
	Token string `json:"token"`
}

type listResponse struct {
	NoteIDs []string `json:"note_ids"`
}

type exportRequest struct {
	Token string `json:"token"`
}

type exportResponse struct {
	Blob string `json:"blob"`
}

const (
	codeOK             = 0
	codeBadRequest     = 4001
	codeUnknownToken   = 4002
	codeUnknownNote    = 4003
	codeBadCiphertext  = 4004
	codeBadMAC         = 4005
	codeInternalError  = 5001
	codeNotImplemented = 5002
)

func reasonForCode(c int) string {
	switch c {
	case codeOK:
		return "ok"
	case codeBadRequest:
		return "bad request"
	case codeUnknownToken:
		return "unknown token"
	case codeUnknownNote:
		return "unknown note id"
	case codeBadCiphertext:
		return "bad ciphertext"
	case codeBadMAC:
		return "mac verification failed"
	case codeInternalError:
		return "internal error"
	case codeNotImplemented:
		return "not implemented"
	}
	return "unknown error"
}
