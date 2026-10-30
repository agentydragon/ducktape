package proxy

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"regexp"
	"strconv"
	"strings"
	"time"
)

const (
	// ScopeHoldSeconds is the long-poll window before a not-yet-folded thread returns no content.
	ScopeHoldSeconds = 20 * time.Second
	// SubsetRowLimit bounds one entity snapshot.
	SubsetRowLimit = 200
	// SubsetBodyLimit bounds one payload-chunk snapshot.
	SubsetBodyLimit = 100
	// MaxSubsetRequestBytes bounds JSON subset request parsing.
	MaxSubsetRequestBytes = 1 << 20
)

const (
	entityColumns = "thread_id,projection_epoch,entity_kind,entity_id,entity_index,cursor,revision_cursor,pending,turn_id,state,text_ref,arguments_ref,output_ref,input_ref"
	chunkColumns  = "thread_id,projection_epoch,owner_cursor,owner_id,field,generation,chunk_index,text"
	entityWhere   = "thread_id = $1 AND projection_epoch = $2"
	chunkWhere    = "thread_id = $1 AND projection_epoch = $2 AND field = $3"
	entityOrder   = "entity_index DESC"
)

var (
	passthroughQuery = map[string]struct{}{
		"offset": {}, "handle": {}, "live": {}, "live_sse": {}, "experimental_live_sse": {},
		"cursor": {}, "log": {}, "expired_handle": {}, "cache-buster": {},
	}
	responseHeaders = map[string]struct{}{
		"cache-control": {}, "content-encoding": {}, "content-type": {}, "electric-cursor": {},
		"electric-handle": {}, "electric-has-data": {}, "electric-internal-known-error": {},
		"electric-offset": {}, "electric-schema": {}, "electric-snapshot": {},
		"electric-up-to-date": {}, "etag": {}, "retry-after": {}, "vary": {},
	}
	bodyPairPattern = regexp.MustCompile(`\(owner_id = \$[0-9]+ AND generation = \$[0-9]+\)`)
	threadIDPattern = regexp.MustCompile(`^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$`)
)

// ThreadScope is the version and fold cursor a client uses to pin all its shape requests.
type ThreadScope struct {
	ProjectionEpoch string `json:"projection_epoch"`
	ThroughCursor   string `json:"through_cursor"`
}

// ThreadScopeStore is the integration point for the future thread store. SubscribeThreadChanges
// must register before returning; its channel should wake a scope waiter after the thread changes.
// The cancel function releases that subscription. Notifications may be coalesced because the
// ElectricProxy rereads CurrentScope after every wakeup.
type ThreadScopeStore interface {
	ThreadExists(context.Context, string) (bool, error)
	CurrentScope(context.Context, string) (ThreadScope, bool, error)
	SubscribeThreadChanges(context.Context, string) (<-chan struct{}, func())
}

// SubsetRequest is the constrained subset shape accepted by Electric POST requests.
type SubsetRequest struct {
	Where   *string           `json:"where,omitempty"`
	Params  map[string]string `json:"params,omitempty"`
	OrderBy *string           `json:"order_by,omitempty"`
	Limit   *int              `json:"limit,omitempty"`
}

// ElectricProxy forwards the app's pinned thread shapes to Electric.
type ElectricProxy struct {
	upstream *UpstreamProxy
	store    ThreadScopeStore
	hold     time.Duration
}

// NewElectricProxy creates the Electric sync handler. client should be shared with other callers
// of the upstream service; the proxy applies the same streaming/no-redirect semantics as UpstreamProxy.
func NewElectricProxy(client *http.Client, electricURL string, store ThreadScopeStore, hold time.Duration) (*ElectricProxy, error) {
	if store == nil {
		return nil, errors.New("thread scope store is required")
	}
	if hold < 0 {
		return nil, errors.New("scope hold must not be negative")
	}
	upstream, err := NewUpstreamProxy(client, electricURL)
	if err != nil {
		return nil, err
	}
	if hold == 0 {
		hold = ScopeHoldSeconds
	}
	return &ElectricProxy{upstream: upstream, store: store, hold: hold}, nil
}

// Register mounts the authenticated-by-caller sync routes on mux. Callers should mount this handler
// behind the app's operator/session authorization middleware.
func (p *ElectricProxy) Register(mux *http.ServeMux) {
	mux.HandleFunc("GET /threads/{thread_id}/sync/scope", p.scope)
	mux.HandleFunc("GET /threads/{thread_id}/sync/entities", p.entities)
	mux.HandleFunc("POST /threads/{thread_id}/sync/entities", p.entities)
	mux.HandleFunc("GET /threads/{thread_id}/sync/chunks/{field}", p.chunks)
	mux.HandleFunc("POST /threads/{thread_id}/sync/chunks/{field}", p.chunks)
}

// Handler returns a standalone handler for all thread sync routes.
func (p *ElectricProxy) Handler() http.Handler {
	mux := http.NewServeMux()
	p.Register(mux)
	return mux
}

func (p *ElectricProxy) scope(w http.ResponseWriter, r *http.Request) {
	threadID, ok := parseThreadID(w, r)
	if !ok {
		return
	}
	exists, err := p.store.ThreadExists(r.Context(), threadID)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "thread scope is unavailable")
		return
	}
	if !exists {
		writeError(w, http.StatusNotFound, "no thread "+threadID)
		return
	}
	changes, unsubscribe := p.store.SubscribeThreadChanges(r.Context(), threadID)
	if unsubscribe != nil {
		defer unsubscribe()
	}
	timer := time.NewTimer(p.hold)
	defer timer.Stop()
	for {
		scope, found, err := p.store.CurrentScope(r.Context(), threadID)
		if err != nil {
			writeError(w, http.StatusInternalServerError, "thread scope is unavailable")
			return
		}
		if found {
			writeJSON(w, http.StatusOK, scope)
			return
		}
		select {
		case <-r.Context().Done():
			return
		case <-timer.C:
			w.WriteHeader(http.StatusNoContent)
			return
		case _, open := <-changes:
			if !open {
				changes = nil
			}
		}
	}
}

func (p *ElectricProxy) entities(w http.ResponseWriter, r *http.Request) {
	threadID, epoch, subset, ok := p.prepareShape(w, r)
	if !ok {
		return
	}
	if subset != nil {
		if err := checkEntitySubset(*subset); err != nil {
			writeError(w, http.StatusBadRequest, err.Error())
			return
		}
	}
	p.forward(w, r, threadID, "thread_entity", entityColumns, entityWhere,
		map[string]string{"1": threadID, "2": epoch}, subset)
}

func (p *ElectricProxy) chunks(w http.ResponseWriter, r *http.Request) {
	threadID, epoch, subset, ok := p.prepareShape(w, r)
	if !ok {
		return
	}
	field := r.PathValue("field")
	switch field {
	case "text", "arguments", "output", "confirmed_input", "command_input":
	default:
		writeError(w, http.StatusUnprocessableEntity, "unknown payload field")
		return
	}
	if subset != nil {
		if err := checkBodySubset(*subset); err != nil {
			writeError(w, http.StatusBadRequest, err.Error())
			return
		}
	}
	p.forward(w, r, threadID, "thread_payload_chunk", chunkColumns, chunkWhere,
		map[string]string{"1": threadID, "2": epoch, "3": field}, subset)
}

func (p *ElectricProxy) prepareShape(w http.ResponseWriter, r *http.Request) (string, string, *SubsetRequest, bool) {
	threadID, ok := parseThreadID(w, r)
	if !ok {
		return "", "", nil, false
	}
	query := r.URL.Query()
	epochs := query["projection_epoch"]
	if len(epochs) != 1 {
		writeError(w, http.StatusUnprocessableEntity, "projection_epoch is required exactly once")
		return "", "", nil, false
	}
	scope, found, err := p.store.CurrentScope(r.Context(), threadID)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "thread scope is unavailable")
		return "", "", nil, false
	}
	if !found || scope.ProjectionEpoch != epochs[0] {
		writeError(w, http.StatusGone, "the selected thread scope is unavailable")
		return "", "", nil, false
	}
	if r.Method == http.MethodGet {
		return threadID, epochs[0], nil, true
	}
	if r.Method != http.MethodPost {
		writeError(w, http.StatusMethodNotAllowed, "method not allowed")
		return "", "", nil, false
	}
	subset, statusCode, err := decodeSubset(w, r)
	if err != nil {
		writeError(w, statusCode, err.Error())
		return "", "", nil, false
	}
	return threadID, epochs[0], &subset, true
}

func (p *ElectricProxy) forward(
	w http.ResponseWriter,
	r *http.Request,
	threadID, table, columns, where string,
	pinnedParams map[string]string,
	subset *SubsetRequest,
) {
	query, err := shapeQuery(r, table, columns, where, pinnedParams)
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	var body []byte
	method := http.MethodGet
	if subset != nil {
		encoded, err := json.Marshal(subset)
		if err != nil {
			writeError(w, http.StatusBadRequest, "invalid subset")
			return
		}
		body = encoded
		method = http.MethodPost
	}
	headers := make(http.Header)
	if accept := r.Header.Get("Accept"); accept != "" {
		headers.Set("Accept", accept)
	} else {
		headers.Set("Accept", "application/json")
	}
	for _, name := range []string{"Electric-Protocol-Version", "If-None-Match"} {
		if value := r.Header.Get(name); value != "" {
			headers.Set(name, value)
		}
	}
	if subset != nil {
		headers.Set("Content-Type", "application/json")
	}
	p.upstream.Relay(w, r, RelayRequest{
		Method: method, Path: "v1/shape", Query: query, Header: headers, Body: body,
		ResponseHeaders: responseHeaders, CacheControl: "private, no-cache",
		OnError: func(w http.ResponseWriter, r *http.Request, _ error) {
			if r.Context().Err() == nil {
				writeError(w, http.StatusServiceUnavailable, "thread sync is unavailable")
			}
		},
	})
}

func shapeQuery(r *http.Request, table, columns, where string, params map[string]string) (string, error) {
	var pairs []string
	seen := make(map[string]struct{})
	for _, rawPair := range strings.Split(r.URL.RawQuery, "&") {
		if rawPair == "" {
			continue
		}
		key, rawValue, hasValue := strings.Cut(rawPair, "=")
		decodedKey, err := url.QueryUnescape(key)
		if err != nil {
			return "", errors.New("malformed sync query")
		}
		value := ""
		if hasValue {
			value, err = url.QueryUnescape(rawValue)
			if err != nil {
				return "", errors.New("malformed sync query")
			}
		}
		if decodedKey == "projection_epoch" {
			if _, duplicate := seen[decodedKey]; duplicate {
				return "", errors.New("duplicate projection_epoch parameter")
			}
			seen[decodedKey] = struct{}{}
			continue
		}
		if _, allowed := passthroughQuery[decodedKey]; !allowed {
			return "", fmt.Errorf("unsupported sync parameter: %s", decodedKey)
		}
		if _, duplicate := seen[decodedKey]; duplicate {
			return "", fmt.Errorf("duplicate sync parameter: %s", decodedKey)
		}
		seen[decodedKey] = struct{}{}
		if decodedKey == "log" {
			if value != "changes_only" {
				return "", errors.New("the selected sync log must be changes_only")
			}
			continue
		}
		pairs = append(pairs, url.QueryEscape(decodedKey)+"="+url.QueryEscape(value))
	}
	for _, pair := range []struct{ key, value string }{
		{"table", table},
		{"columns", columns},
		{"where", where},
		{"replica", "full"},
		{"log", "changes_only"},
		{"queryable_columns", columns},
	} {
		pairs = append(pairs, url.QueryEscape(pair.key)+"="+url.QueryEscape(pair.value))
	}
	for index := 1; index <= len(params); index++ {
		key := strconv.Itoa(index)
		value, ok := params[key]
		if !ok {
			return "", errors.New("internal pinned parameters are not sequential")
		}
		paramName := "params[" + key + "]"
		pairs = append(pairs, url.QueryEscape(paramName)+"="+url.QueryEscape(value))
	}
	return strings.Join(pairs, "&"), nil
}

func checkEntitySubset(subset SubsetRequest) error {
	where := ""
	if subset.Where != nil {
		where = *subset.Where
	}
	allowed := map[string]struct{}{
		"entity_index < $1": {}, "entity_kind = 'view_state'": {},
		"entity_kind = 'command' AND pending = true":      {},
		"entity_kind = 'command' AND entity_id = ANY($1)": {},
	}
	validWhere := subset.Where == nil
	if subset.Where != nil {
		_, validWhere = allowed[where]
	}
	if !validWhere || subset.OrderBy == nil || *subset.OrderBy != entityOrder {
		return fmt.Errorf("unsupported subset: where=%q order_by=%v", where, subset.OrderBy)
	}
	if subset.Limit == nil || *subset.Limit < 1 || *subset.Limit > SubsetRowLimit {
		return fmt.Errorf("a subset reads between 1 and %d rows", SubsetRowLimit)
	}
	params := subset.Params
	if params == nil {
		params = map[string]string{}
	}
	wantParam := where == "entity_index < $1" || where == "entity_kind = 'command' AND entity_id = ANY($1)"
	if (wantParam && len(params) != 1) || (!wantParam && len(params) != 0) {
		return errors.New("subset parameters do not match its form")
	}
	if wantParam {
		value, ok := params["1"]
		if !ok {
			return errors.New("subset parameters do not match its form")
		}
		if where == "entity_index < $1" {
			for _, digit := range value {
				if digit < '0' || digit > '9' {
					return errors.New("an index bound is a non-negative integer")
				}
			}
		}
		if where == "entity_index < $1" && value == "" {
			return errors.New("an index bound is a non-negative integer")
		}
	}
	return nil
}

func checkBodySubset(subset SubsetRequest) error {
	where := ""
	if subset.Where != nil {
		where = *subset.Where
	}
	matches := bodyPairPattern.FindAllString(where, -1)
	if len(matches) == 0 {
		return errors.New("a body subset names (owner_id, generation) pairs")
	}
	if len(matches) > SubsetBodyLimit {
		return fmt.Errorf("a subset reads at most %d bodies", SubsetBodyLimit)
	}
	var expected strings.Builder
	for index := range matches {
		if index != 0 {
			expected.WriteString(" OR ")
		}
		fmt.Fprintf(&expected, "(owner_id = $%d AND generation = $%d)", 2*index+1, 2*index+2)
	}
	if where != expected.String() || subset.OrderBy != nil || subset.Limit != nil {
		return errors.New("a body subset names (owner_id, generation) pairs")
	}
	params := subset.Params
	if params == nil {
		params = map[string]string{}
	}
	if len(params) != 2*len(matches) {
		return errors.New("subset parameters do not match its form")
	}
	for index := 1; index <= 2*len(matches); index++ {
		if _, ok := params[strconv.Itoa(index)]; !ok {
			return errors.New("subset parameters do not match its form")
		}
	}
	for index := 1; index <= len(matches); index++ {
		value := params[strconv.Itoa(2*index)]
		if value == "" {
			return errors.New("a generation is a non-negative integer")
		}
		for _, digit := range value {
			if digit < '0' || digit > '9' {
				return errors.New("a generation is a non-negative integer")
			}
		}
	}
	return nil
}

func decodeSubset(w http.ResponseWriter, r *http.Request) (SubsetRequest, int, error) {
	decoder := json.NewDecoder(http.MaxBytesReader(w, r.Body, MaxSubsetRequestBytes))
	decoder.DisallowUnknownFields()
	var subset SubsetRequest
	if err := decoder.Decode(&subset); err != nil {
		var tooLarge *http.MaxBytesError
		if errors.As(err, &tooLarge) {
			return SubsetRequest{}, http.StatusRequestEntityTooLarge, errors.New("subset request is too large")
		}
		return SubsetRequest{}, http.StatusUnprocessableEntity, errors.New("invalid subset request")
	}
	var trailing any
	if err := decoder.Decode(&trailing); !errors.Is(err, io.EOF) {
		return SubsetRequest{}, http.StatusUnprocessableEntity, errors.New("invalid subset request")
	}
	return subset, http.StatusOK, nil
}

func parseThreadID(w http.ResponseWriter, r *http.Request) (string, bool) {
	threadID := r.PathValue("thread_id")
	if !threadIDPattern.MatchString(threadID) {
		writeError(w, http.StatusUnprocessableEntity, "thread_id must be a UUID")
		return "", false
	}
	return strings.ToLower(threadID), true
}

func writeError(w http.ResponseWriter, statusCode int, detail string) {
	writeJSON(w, statusCode, map[string]string{"detail": detail})
}

func writeJSON(w http.ResponseWriter, statusCode int, value any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(statusCode)
	_ = json.NewEncoder(w).Encode(value)
}
