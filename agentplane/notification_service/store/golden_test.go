package store

import (
	"encoding/json"
	"errors"
	"testing"
)

func TestDestinationKeysAreThosePythonComputes(t *testing.T) {
	for _, vector := range loadGolden(t).DestinationKeys {
		t.Run(vector.Name, func(t *testing.T) {
			if got := DestinationKey(vector.DestinationRef, vector.SessionID); got != vector.Key {
				t.Errorf("DestinationKey = %s, want %s", got, vector.Key)
			}
		})
	}
}

func TestCreationsAreReadAndWrittenAsPythonDoes(t *testing.T) {
	for _, vector := range loadGolden(t).Subscriptions {
		t.Run(vector.Name, func(t *testing.T) {
			var sent, stored Subscribe
			if err := decode(vector.Input, &sent); err != nil {
				t.Fatalf("decoding the input: %v", err)
			}
			if err := sent.Validate(); err != nil {
				t.Fatalf("Validate: %v", err)
			}
			written, err := json.Marshal(sent)
			if err != nil {
				t.Fatal(err)
			}
			sameJSON(t, written, vector.Stored)

			// What Python stored reads back, without loss, as the creation that was sent.
			if err := decode(vector.Stored, &stored); err != nil {
				t.Fatalf("decoding the stored form: %v", err)
			}
			if !stored.Equal(sent) {
				t.Errorf("stored form %+v is not the creation sent %+v", stored, sent)
			}
			rewritten, err := json.Marshal(stored)
			if err != nil {
				t.Fatal(err)
			}
			sameJSON(t, rewritten, vector.Stored)
		})
	}
}

func TestCreationsPythonRefusesAreRefused(t *testing.T) {
	for _, vector := range loadGolden(t).InvalidSubscriptions {
		t.Run(vector.Name, func(t *testing.T) {
			var creation Subscribe
			err := decode(vector.Input, &creation)
			if err == nil {
				err = creation.Validate()
				if err != nil && !errors.Is(err, ErrInvalid) {
					t.Errorf("Validate error %v is not ErrInvalid", err)
				}
			}
			if err == nil {
				t.Errorf("accepted %s", vector.Input)
			}
		})
	}
}

func TestCreationsAreTheSameWhereThePythonCheckSaysSo(t *testing.T) {
	for _, vector := range loadGolden(t).Comparisons {
		t.Run(vector.Name, func(t *testing.T) {
			var a, b Subscribe
			if err := errors.Join(decode(vector.A, &a), decode(vector.B, &b)); err != nil {
				t.Fatal(err)
			}
			if got := a.Equal(b); got != vector.Same {
				t.Errorf("a.Equal(b) = %v, want %v", got, vector.Same)
			}
			if got := b.Equal(a); got != vector.Same {
				t.Errorf("b.Equal(a) = %v, want %v", got, vector.Same)
			}
		})
	}
}

func TestEventsAreReadAndWrittenAsPythonDoes(t *testing.T) {
	for _, vector := range loadGolden(t).Events {
		t.Run(vector.Name, func(t *testing.T) {
			event, err := DecodeEventIdentity(vector.Event)
			if err != nil {
				t.Fatal(err)
			}
			written, err := json.Marshal(event)
			if err != nil {
				t.Fatal(err)
			}
			sameJSON(t, written, vector.Event)
		})
	}
}
