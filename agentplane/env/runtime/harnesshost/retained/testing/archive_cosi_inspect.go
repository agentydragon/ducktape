// Test-only verifier for a Session record in an archived Runtime bbolt file.
package main

import (
	"errors"
	"fmt"
	"os"
	"strings"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/harnesshost/retained"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	"github.com/cosi-project/runtime/pkg/state/impl/store"
	"go.etcd.io/bbolt"
)

func inspect(path, id, expected string) error {
	db, err := bbolt.Open(path, 0o600, &bbolt.Options{ReadOnly: true})
	if err != nil {
		return err
	}
	defer db.Close()
	return db.View(func(tx *bbolt.Tx) error {
		namespace := tx.Bucket([]byte(lifecycle.Namespace))
		if namespace == nil {
			return errors.New("Runtime namespace missing")
		}
		kind := namespace.Bucket([]byte(retained.SessionRecordType))
		if kind == nil {
			return errors.New("SessionRecords bucket missing")
		}
		encoded := kind.Get([]byte(id))
		if encoded == nil {
			return fmt.Errorf("Session record %q missing", id)
		}
		value, err := (store.ProtobufMarshaler{}).UnmarshalResource(encoded)
		if err != nil {
			return err
		}
		record, ok := value.(*retained.SessionRecord)
		if !ok || record.Metadata().Namespace() != lifecycle.Namespace || record.Metadata().ID() != id || record.Metadata().Type() != retained.SessionRecordType {
			return fmt.Errorf("incorrect archived COSI resource: %T", value)
		}
		if !strings.Contains(record.TypedSpec().Value.GetInstructions(), expected) {
			return errors.New("archived Session record lacks expected instructions")
		}
		return nil
	})
}

func main() {
	if len(os.Args) != 4 {
		fmt.Fprintln(os.Stderr, "usage: archive_cosi_inspect DB SESSION EXPECTED_INSTRUCTIONS")
		os.Exit(2)
	}
	if err := inspect(os.Args[1], os.Args[2], os.Args[3]); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
