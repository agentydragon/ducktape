package centralsampler

import (
	"context"
	"database/sql"
	"fmt"

	samplerpb "github.com/agentydragon/ducktape/agentplane/central_sampler/samplerpb"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/pressly/goose/v3"
	"google.golang.org/protobuf/encoding/protowire"
	"google.golang.org/protobuf/proto"
)

// Convert retained request identities before the new sampler handles any retry. This changes no
// conversation, command, cursor, or effect; only the immutable request representation changes.
var sessionPlacementMigration = goose.NewGoMigration(10,
	&goose.GoFunc{RunTx: func(ctx context.Context, tx *sql.Tx) error {
		rows, err := tx.QueryContext(ctx, "SELECT id,configuration FROM sampler_session")
		if err != nil {
			return err
		}
		type update struct {
			id            string
			configuration []byte
		}
		var updates []update
		for rows.Next() {
			var id string
			var raw []byte
			if err := rows.Scan(&id, &raw); err != nil {
				rows.Close()
				return err
			}
			request := &samplerpb.CreateSessionRequest{}
			if err := proto.Unmarshal(raw, request); err != nil {
				rows.Close()
				return fmt.Errorf("decode retained central Session %s: %w", id, err)
			}
			spec := request.GetSpec()
			executorChanged, err := migrateRetainedExecutor(request.GetExecutor())
			if err != nil {
				rows.Close()
				return fmt.Errorf("retained central Session %s has invalid executor: %w", id, err)
			}
			executor := request.GetExecutor().GetTarget()
			if spec == nil || executor.GetNamespace() == "" || executor.GetName() == "" || executor.GetUid() == "" {
				rows.Close()
				return fmt.Errorf("retained central Session %s lacks exact executor identity", id)
			}
			harness, other, err := consumeHistoricalHarness(spec.ProtoReflect().GetUnknown())
			if err != nil {
				rows.Close()
				return fmt.Errorf("retained central Session %s has invalid harness: %w", id, err)
			}
			if existing := spec.GetPlacement().GetCentralSamplerV1(); existing != nil {
				if harness != sessionpb.Harness_HARNESS_UNSPECIFIED || existing.GetExecutorEnvironment().GetName() != executor.GetName() ||
					existing.GetExecutorEnvironment().GetUid() != executor.GetUid() {
					rows.Close()
					return fmt.Errorf("retained central Session %s has conflicting placement", id)
				}
				if !executorChanged {
					continue
				}
			} else {
				if harness != sessionpb.Harness_HARNESS_CENTRAL_SAMPLER {
					rows.Close()
					return fmt.Errorf("retained central Session %s has invalid harness %v", id, harness)
				}
				spec.Placement = &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_CentralSamplerV1{CentralSamplerV1: &sessionpb.CentralSamplerV1Placement{
					ExecutorEnvironment: &sessionpb.ExactEnvironment{Name: executor.GetName(), Uid: executor.GetUid()},
				}}}
				spec.ProtoReflect().SetUnknown(other)
			}
			converted, err := proto.MarshalOptions{Deterministic: true}.Marshal(request)
			if err != nil {
				rows.Close()
				return err
			}
			updates = append(updates, update{id, converted})
		}
		if err := rows.Err(); err != nil {
			rows.Close()
			return err
		}
		if err := rows.Close(); err != nil {
			return err
		}
		for _, row := range updates {
			if _, err := tx.ExecContext(ctx, "UPDATE sampler_session SET configuration=$2 WHERE id=$1", row.id, row.configuration); err != nil {
				return err
			}
		}
		return nil
	}},
	&goose.GoFunc{RunTx: func(context.Context, *sql.Tx) error {
		return fmt.Errorf("retained Session placement conversion is irreversible")
	}})

// The retained request predates identity.proto: executor fields 2 and 3 held the
// Sandbox name and UID, and its owner's namespace was the Sandbox namespace.
func migrateRetainedExecutor(destination *identitypb.SandboxDestination) (bool, error) {
	if destination == nil {
		return false, fmt.Errorf("missing destination")
	}
	data := destination.ProtoReflect().GetUnknown()
	var name, uid string
	var rest []byte
	for len(data) > 0 {
		number, kind, n := protowire.ConsumeTag(data)
		if n < 0 {
			return false, protowire.ParseError(n)
		}
		length := protowire.ConsumeFieldValue(number, kind, data[n:])
		if length < 0 {
			return false, protowire.ParseError(length)
		}
		if number == 2 || number == 3 {
			if kind != protowire.BytesType {
				return false, fmt.Errorf("historical destination field %d has wrong wire type", number)
			}
			value, consumed := protowire.ConsumeString(data[n:])
			if consumed < 0 {
				return false, protowire.ParseError(consumed)
			}
			if number == 2 {
				name = value
			} else {
				uid = value
			}
		} else {
			rest = append(rest, data[:n+length]...)
		}
		data = data[n+length:]
	}
	if name == "" && uid == "" {
		return false, nil
	}
	namespace := destination.GetOwner().GetNamespace()
	if namespace == "" || name == "" || uid == "" {
		return false, fmt.Errorf("incomplete historical destination")
	}
	if current := destination.GetTarget(); current != nil && (current.GetNamespace() != namespace || current.GetName() != name || current.GetUid() != uid) {
		return false, fmt.Errorf("conflicting historical destination")
	}
	destination.Target = &identitypb.EnvironmentTarget{Namespace: namespace, Name: name, Uid: uid}
	destination.ProtoReflect().SetUnknown(rest)
	return true, nil
}

func consumeHistoricalHarness(data []byte) (sessionpb.Harness, []byte, error) {
	var harness sessionpb.Harness
	var rest []byte
	for len(data) > 0 {
		number, kind, n := protowire.ConsumeTag(data)
		if n < 0 {
			return 0, nil, protowire.ParseError(n)
		}
		length := protowire.ConsumeFieldValue(number, kind, data[n:])
		if length < 0 {
			return 0, nil, protowire.ParseError(length)
		}
		if number == 1 {
			if kind != protowire.VarintType {
				return 0, nil, fmt.Errorf("legacy harness tag has wrong wire type")
			}
			value, _ := protowire.ConsumeVarint(data[n:])
			harness = sessionpb.Harness(value)
		} else {
			rest = append(rest, data[:n+length]...)
		}
		data = data[n+length:]
	}
	return harness, rest, nil
}
