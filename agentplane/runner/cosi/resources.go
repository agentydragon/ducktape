// Package cosi contains mechanical helpers for typed Runner COSI resources.
package cosi

import (
	"context"

	"github.com/cosi-project/runtime/pkg/controller"
	"github.com/cosi-project/runtime/pkg/controller/generic"
	"github.com/cosi-project/runtime/pkg/safe"
	"github.com/cosi-project/runtime/pkg/state"
)

type SpecResource[T any] interface {
	generic.ResourceWithRD
	TypedSpec() *T
}

func Read[T any, R SpecResource[T]](ctx context.Context, reader controller.Reader, target R) (T, bool, error) {
	var zero T
	value, err := safe.ReaderGet[R](ctx, reader, target.Metadata())
	if state.IsNotFoundError(err) {
		return zero, false, nil
	}
	if err != nil {
		return zero, false, err
	}

	return *value.TypedSpec(), true, nil
}

func Write[T comparable, R SpecResource[T]](ctx context.Context, writer controller.ReaderWriter, target R, value T) (bool, error) {
	old, found, err := Read(ctx, writer, target)
	if err != nil || found && old == value {
		return false, err
	}

	err = safe.WriterModify(ctx, writer, target, func(current R) error {
		*current.TypedSpec() = value
		return nil
	})

	return err == nil, err
}

func Publish[T comparable, R SpecResource[T]](ctx context.Context, writer state.State, target R, value T, owner string) error {
	return safe.StateModify(ctx, writer, target, func(current R) error {
		*current.TypedSpec() = value
		return nil
	}, state.WithUpdateOwner(owner))
}
