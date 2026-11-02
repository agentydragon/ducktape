package retained

import (
	"context"
	"errors"
	"fmt"
	"sort"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/cosistate"
	persistencepb "github.com/agentydragon/ducktape/agentplane/env/runtime/cosistate/persistentpb"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	delegationpb "github.com/agentydragon/ducktape/agentplane/proto/delegation"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/cosi-project/runtime/pkg/controller"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/resource/meta/spec"
	resourcepb "github.com/cosi-project/runtime/pkg/resource/protobuf"
	"github.com/cosi-project/runtime/pkg/resource/typed"
	"github.com/cosi-project/runtime/pkg/safe"
	"github.com/cosi-project/runtime/pkg/state"
	"google.golang.org/protobuf/proto"
)

const (
	SessionRecordType = "SessionRecords.agentplane"
	IngressAckType    = "IngressAcks.agentplane"
)

type sessionRecordExtension struct{}

func (sessionRecordExtension) ResourceDefinition() spec.ResourceDefinitionSpec {
	return spec.ResourceDefinitionSpec{Type: SessionRecordType, DefaultNamespace: lifecycle.Namespace}
}

type (
	sessionSpec   = resourcepb.ResourceSpec[persistencepb.SessionRecordSpec, *persistencepb.SessionRecordSpec]
	SessionRecord = typed.Resource[sessionSpec, sessionRecordExtension]
)

type ingressAckExtension struct{}

func (ingressAckExtension) ResourceDefinition() spec.ResourceDefinitionSpec {
	return spec.ResourceDefinitionSpec{Type: IngressAckType, DefaultNamespace: lifecycle.Namespace}
}

type (
	acknowledgementSpec = resourcepb.ResourceSpec[persistencepb.IngressAckSpec, *persistencepb.IngressAckSpec]
	IngressAckResource  = typed.Resource[acknowledgementSpec, ingressAckExtension]
)

func init() {
	if err := resourcepb.RegisterResource(SessionRecordType, &SessionRecord{}); err != nil {
		panic(err)
	}
	if err := resourcepb.RegisterResource(IngressAckType, &IngressAckResource{}); err != nil {
		panic(err)
	}
}

// StorageError identifies a failed retained resource operation so native frame
// handling does not treat a durable write failure as a translation error.
type StorageError struct{ Err error }

func (e *StorageError) Error() string { return "Session record storage: " + e.Err.Error() }
func (e *StorageError) Unwrap() error { return e.Err }

func SessionRecordRegistration() cosistate.Registration {
	return cosistate.Registration{Type: SessionRecordType, Validate: validateSessionResource}
}

func IngressAckRegistration() cosistate.Registration {
	return cosistate.Registration{Type: IngressAckType, Validate: validateAcknowledgementResource}
}

func AcknowledgementResource(id string, acknowledgement Acknowledgement) *IngressAckResource {
	return typed.NewResource[acknowledgementSpec, ingressAckExtension](resource.NewMetadata(lifecycle.Namespace, IngressAckType, id, resource.VersionUndefined),
		resourcepb.NewResourceSpec(&persistencepb.IngressAckSpec{Cursor: acknowledgement.Cursor, Sealed: acknowledgement.Sealed}))
}

func validateAcknowledgementResource(value resource.Resource) error {
	entry, ok := value.(*IngressAckResource)
	if !ok || entry.Metadata().Namespace() != lifecycle.Namespace || entry.Metadata().Type() != IngressAckType || entry.Metadata().Owner() != lifecycle.HostOwner || entry.TypedSpec().Value == nil {
		return fmt.Errorf("unexpected ingress acknowledgement resource %T", value)
	}
	_, err := ValidateSessionID(entry.Metadata().ID())
	return err
}

func ReadAcknowledgement(ctx context.Context, reader controller.Reader, id string) (Acknowledgement, bool, error) {
	if _, err := ValidateSessionID(id); err != nil {
		return Acknowledgement{}, false, err
	}
	value, err := safe.ReaderGet[*IngressAckResource](ctx, reader, AcknowledgementResource(id, Acknowledgement{}).Metadata())
	if state.IsNotFoundError(err) {
		return Acknowledgement{}, false, nil
	}
	if err != nil {
		return Acknowledgement{}, false, err
	}
	if err := validateAcknowledgementResource(value); err != nil {
		return Acknowledgement{}, false, err
	}
	return Acknowledgement{Cursor: value.TypedSpec().Value.Cursor, Sealed: value.TypedSpec().Value.Sealed}, true, nil
}

func SessionResource(id string, record Record) *SessionRecord {
	return typed.NewResource[sessionSpec, sessionRecordExtension](resource.NewMetadata(lifecycle.Namespace, SessionRecordType, id, resource.VersionUndefined),
		resourcepb.NewResourceSpec(recordToProto(record)))
}

func recordToProto(record Record) *persistencepb.SessionRecordSpec {
	value := &persistencepb.SessionRecordSpec{
		Harness: record.Harness, Cwd: record.CWD, Model: record.Model,
		ReasoningEffort: record.ReasoningEffort, Instructions: record.Instructions, SetupScript: []byte(record.SetupScript),
		EventSourceId: record.EventSourceID, ExecutionPrincipal: record.ExecutionPrincipal,
	}
	if record.TotalContextBudgetTokens != nil {
		budget := int64(*record.TotalContextBudgetTokens)
		value.TotalContextBudgetTokens = &budget
	}
	if record.NativeSessionID != nil {
		native := *record.NativeSessionID
		value.NativeSessionId = &native
	}
	return value
}

func recordFromProto(value *persistencepb.SessionRecordSpec) (Record, error) {
	if value == nil {
		return Record{}, errors.New("missing persisted Session record")
	}
	record := Record{
		Harness: value.Harness, CWD: value.Cwd, Model: value.Model, ReasoningEffort: value.ReasoningEffort,
		Instructions: value.Instructions, SetupScript: string(value.SetupScript), EventSourceID: value.EventSourceId,
		NativeSessionID: value.NativeSessionId, ExecutionPrincipal: value.ExecutionPrincipal,
	}
	if value.TotalContextBudgetTokens != nil {
		budget := int(*value.TotalContextBudgetTokens)
		if int64(budget) != *value.TotalContextBudgetTokens {
			return Record{}, errors.New("persisted context budget overflows int")
		}
		record.TotalContextBudgetTokens = &budget
	}
	return record, validateRecord(record)
}

func validateSessionResource(value resource.Resource) error {
	entry, ok := value.(*SessionRecord)
	if !ok || entry.Metadata().Namespace() != lifecycle.Namespace || entry.Metadata().Type() != SessionRecordType || entry.Metadata().Owner() != lifecycle.HostOwner {
		return fmt.Errorf("unexpected Session resource %T", value)
	}
	if _, err := ValidateSessionID(entry.Metadata().ID()); err != nil {
		return err
	}
	_, err := recordFromProto(entry.TypedSpec().Value)
	return err
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
	record, err := recordFromProto(value.TypedSpec().Value)
	return record.DeepCopy(), err
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
		value.TypedSpec().Value = recordToProto(record.DeepCopy())
		return nil
	}, state.WithUpdateOwner(lifecycle.HostOwner))
}

func (s *Store) update(id string, change func(*Record)) error {
	if s.state == nil {
		return &StorageError{Err: errors.New("Session COSI state is required")}
	}
	if _, err := ValidateSessionID(id); err != nil {
		return err
	}
	_, err := safe.StateUpdateWithConflicts(context.Background(), s.state, SessionResource(id, Record{}).Metadata(), func(value *SessionRecord) error {
		record, err := recordFromProto(value.TypedSpec().Value)
		if err != nil {
			return err
		}
		change(&record)
		if err := validateRecord(record); err != nil {
			return err
		}
		value.TypedSpec().Value = recordToProto(record)
		return nil
	}, state.WithUpdateOwner(lifecycle.HostOwner))
	if err != nil {
		return &StorageError{Err: err}
	}
	return nil
}

func (s *Store) UpdateModel(id, model string) error {
	return s.update(id, func(record *Record) { record.Model = model })
}

func (s *Store) UpdateReasoningEffort(id, effort string) error {
	return s.update(id, func(record *Record) { record.ReasoningEffort = effort })
}

func (s *Store) UpdateNativeSessionID(id, native string) error {
	return s.update(id, func(record *Record) { record.NativeSessionID = &native })
}
