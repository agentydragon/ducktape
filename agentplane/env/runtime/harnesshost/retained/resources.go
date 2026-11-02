package retained

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"sort"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/cosistate"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	delegationpb "github.com/agentydragon/ducktape/agentplane/proto/delegation"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/resource/meta/spec"
	"github.com/cosi-project/runtime/pkg/resource/typed"
	"github.com/cosi-project/runtime/pkg/safe"
	"github.com/cosi-project/runtime/pkg/state"
	"google.golang.org/protobuf/proto"
)

const SessionRecordType = "SessionRecords.agentplane"

type sessionRecordExtension struct{}

func (sessionRecordExtension) ResourceDefinition() spec.ResourceDefinitionSpec {
	return spec.ResourceDefinitionSpec{Type: SessionRecordType, DefaultNamespace: lifecycle.Namespace}
}

type SessionRecord = typed.Resource[Record, sessionRecordExtension]

func SessionResource(id string, record Record) *SessionRecord {
	return typed.NewResource[Record, sessionRecordExtension](resource.NewMetadata(lifecycle.Namespace, SessionRecordType, id, resource.VersionUndefined), record)
}

func (r Record) DeepCopy() Record {
	if r.TotalContextBudgetTokens != nil {
		value := *r.TotalContextBudgetTokens
		r.TotalContextBudgetTokens = &value
	}
	if r.NativeSessionID != nil {
		value := *r.NativeSessionID
		r.NativeSessionID = &value
	}
	if r.ExecutionPrincipal != nil {
		r.ExecutionPrincipal = proto.Clone(r.ExecutionPrincipal).(*delegationpb.ServiceAccountIdentity)
	}
	return r
}

func validateRecord(r Record) error {
	if r.Harness != sessionpb.Harness_HARNESS_CLAUDE && r.Harness != sessionpb.Harness_HARNESS_CODEX {
		return fmt.Errorf("invalid persisted harness value %d", r.Harness)
	}
	if r.TotalContextBudgetTokens != nil && *r.TotalContextBudgetTokens <= 0 {
		return errors.New("total context budget must be positive")
	}
	if p := r.ExecutionPrincipal; p != nil && (p.GetAccount().GetNamespace() == "" || p.GetAccount().GetName() == "" || p.GetUid() == "") {
		return errors.New("invalid persisted execution principal")
	}
	return nil
}

type sessionRecordDisk struct {
	ID     string `json:"id"`
	Record Record `json:"record"`
}

func SessionRecordCodec() cosistate.Codec {
	return cosistate.Codec{Type: SessionRecordType, Marshal: MarshalSessionResource, Unmarshal: UnmarshalSessionResource}
}

// MarshalSessionResource and UnmarshalSessionResource provide the selected
// resource's durable representation to the Runtime COSI backing store.
func MarshalSessionResource(value resource.Resource) ([]byte, error) {
	entry, ok := value.(*SessionRecord)
	if !ok || entry.Metadata().Namespace() != lifecycle.Namespace {
		return nil, fmt.Errorf("unexpected Session resource %T", value)
	}
	if _, err := ValidateSessionID(entry.Metadata().ID()); err != nil {
		return nil, err
	}
	if err := validateRecord(*entry.TypedSpec()); err != nil {
		return nil, err
	}
	return json.Marshal(sessionRecordDisk{ID: entry.Metadata().ID(), Record: *entry.TypedSpec()})
}

func UnmarshalSessionResource(data []byte) (resource.Resource, error) {
	var disk sessionRecordDisk
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&disk); err != nil {
		return nil, err
	}
	var trailing json.RawMessage
	if err := decoder.Decode(&trailing); err != io.EOF {
		return nil, errors.New("invalid trailing persisted Session resource")
	}
	if _, err := ValidateSessionID(disk.ID); err != nil {
		return nil, err
	}
	if err := validateRecord(disk.Record); err != nil {
		return nil, err
	}
	value := SessionResource(disk.ID, disk.Record)
	value.Metadata().SetVersion(resource.VersionUndefined.Next())
	if err := value.Metadata().SetOwner(lifecycle.HostOwner); err != nil {
		return nil, err
	}
	return value, nil
}

func (s *Store) SessionIDs() ([]string, error) {
	if s.state == nil {
		return nil, errors.New("Session COSI state is required")
	}
	values, err := s.state.List(context.Background(), resource.NewMetadata(lifecycle.Namespace, SessionRecordType, "", resource.VersionUndefined))
	if err != nil {
		return nil, err
	}
	ids := make([]string, 0, len(values.Items))
	for _, value := range values.Items {
		ids = append(ids, value.Metadata().ID())
	}
	sort.Strings(ids)
	return ids, nil
}

func (s *Store) Exists(id string) (bool, error) {
	_, err := s.Read(id)
	if state.IsNotFoundError(err) {
		return false, nil
	}
	return err == nil, err
}

func (s *Store) Read(id string) (Record, error) {
	if s.state == nil {
		return Record{}, errors.New("Session COSI state is required")
	}
	if _, err := ValidateSessionID(id); err != nil {
		return Record{}, err
	}
	value, err := safe.StateGet[*SessionRecord](context.Background(), s.state, SessionResource(id, Record{}).Metadata())
	if err != nil {
		return Record{}, err
	}
	return value.TypedSpec().DeepCopy(), nil
}

func (s *Store) Write(id string, record Record) error {
	if s.state == nil {
		return errors.New("Session COSI state is required")
	}
	if err := validateRecord(record); err != nil {
		return err
	}
	directory, err := s.Directory(id)
	if err != nil {
		return err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if err := EnsureDirectory(directory); err != nil {
		return err
	}
	return safe.StateModify(context.Background(), s.state, SessionResource(id, record), func(value *SessionRecord) error {
		*value.TypedSpec() = record.DeepCopy()
		return nil
	}, state.WithUpdateOwner(lifecycle.HostOwner))
}
