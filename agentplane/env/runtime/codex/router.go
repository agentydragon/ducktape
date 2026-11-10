package codex

import (
	"encoding/json"
	"errors"
	"slices"
	"sync"
)

// Router assigns each frame of a shared app-server to the hosts it concerns. A response goes to the host
// whose request it answers; a frame naming a thread goes to the host of that thread, or of the root
// thread it descends from; any other frame goes to every host.
type Router[H comparable] struct {
	mu       sync.Mutex
	hosts    []H
	requests map[string]routedRequest[H]
	threads  map[string]H
	// starting is the host whose `thread/start` is unanswered. The thread it creates may name itself before
	// the response does, so a frame naming an unknown thread is its.
	starting *H
}

type routedRequest[H comparable] struct {
	host   H
	method string
}

func NewRouter[H comparable]() *Router[H] {
	return &Router[H]{requests: map[string]routedRequest[H]{}, threads: map[string]H{}}
}

// Host adds `host` to the hosts that receive frames.
func (r *Router[H]) Host(host H) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if !slices.Contains(r.hosts, host) {
		r.hosts = append(r.hosts, host)
	}
}

// Release stops routing frames to `host`: responses to its requests and frames of its threads are
// dropped until it is hosted again.
func (r *Router[H]) Release(host H) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.hosts = slices.DeleteFunc(r.hosts, func(hosted H) bool { return hosted == host })
	if r.starting != nil && *r.starting == host {
		r.starting = nil
	}
}

// Hosts returns the hosts in the order they were first hosted.
func (r *Router[H]) Hosts() []H {
	r.mu.Lock()
	defer r.mu.Unlock()
	return slices.Clone(r.hosts)
}

// Sent records a frame `host` writes to the app-server, before it is written, so its response can be
// routed back. A request to resume a thread assigns that thread to `host`.
func (r *Router[H]) Sent(host H, frame json.RawMessage) error {
	var sent struct {
		ID     json.RawMessage `json:"id"`
		Method string          `json:"method"`
		Params struct {
			ThreadID string `json:"threadId"`
		} `json:"params"`
	}
	if err := json.Unmarshal(frame, &sent); err != nil {
		return err
	}
	if len(sent.ID) == 0 || sent.Method == "" {
		return nil
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	if sent.Method == "thread/start" {
		if r.starting != nil {
			return errors.New("another Session's thread/start is still unanswered on the shared Codex app-server")
		}
		r.starting = &host
	}
	if sent.Method == "thread/resume" && sent.Params.ThreadID != "" {
		r.threads[sent.Params.ThreadID] = host
	}
	r.requests[string(sent.ID)] = routedRequest[H]{host: host, method: sent.Method}
	return nil
}

// Route returns the hosts `frame` concerns.
func (r *Router[H]) Route(frame json.RawMessage) []H {
	var received struct {
		ID     json.RawMessage `json:"id"`
		Method string          `json:"method"`
		Params struct {
			ThreadID       string `json:"threadId"`
			ConversationID string `json:"conversationId"`
			Thread         struct {
				ID             string `json:"id"`
				ParentThreadID string `json:"parentThreadId"`
			} `json:"thread"`
		} `json:"params"`
		Result struct {
			Thread struct {
				ID string `json:"id"`
			} `json:"thread"`
		} `json:"result"`
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	if json.Unmarshal(frame, &received) != nil {
		return slices.Clone(r.hosts)
	}
	if len(received.ID) > 0 && received.Method == "" {
		request, ok := r.requests[string(received.ID)]
		if !ok {
			return slices.Clone(r.hosts)
		}
		delete(r.requests, string(received.ID))
		if request.method == "thread/start" {
			r.starting = nil
		}
		if (request.method == "thread/start" || request.method == "thread/resume") && received.Result.Thread.ID != "" {
			r.threads[received.Result.Thread.ID] = request.host
		}
		return r.hosted(request.host)
	}
	thread := received.Params.ThreadID
	if thread == "" {
		thread = received.Params.ConversationID
	}
	if thread == "" {
		thread = received.Params.Thread.ID
	}
	if thread == "" {
		return slices.Clone(r.hosts)
	}
	host, known := r.threads[thread]
	if !known {
		if parent, ok := r.threads[received.Params.Thread.ParentThreadID]; ok {
			host, known = parent, true
		} else if r.starting != nil {
			host, known = *r.starting, true
		}
		if known {
			r.threads[thread] = host
		}
	}
	if !known {
		return slices.Clone(r.hosts)
	}
	return r.hosted(host)
}

func (r *Router[H]) hosted(host H) []H {
	if !slices.Contains(r.hosts, host) {
		return nil
	}
	return []H{host}
}
