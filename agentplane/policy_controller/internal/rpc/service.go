// Package rpc serves the PolicyService.
package rpc

import (
	"context"
	"errors"
	"slices"
	"time"

	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	policypb "github.com/agentydragon/ducktape/agentplane/policy/v1"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/sandboxreader"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"k8s.io/apimachinery/pkg/util/sets"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

const (
	recheckInterval = 5 * time.Second
	writeTimeout    = 5 * time.Second
	snapshotChunk   = 64
)

type Service struct {
	policypb.UnimplementedPolicyServiceServer
	namespace string
	// workloadNamespaces are where the workload-identity issuer serves WorkloadCredentials; none
	// when it is off, since no record is issued then.
	workloadNamespaces []string
	objects            client.WithWatch
	sandboxes          *sandboxreader.Reader
	readers            sets.Set[auth.Account]
	writers            []auth.Account
}

func New(namespace string, workloadNamespaces []string, objects client.WithWatch, readers, writers []auth.Account) (*Service, error) {
	if namespace == "" || objects == nil {
		return nil, errors.New("policy namespace and Kubernetes watch client are required")
	}
	sandboxes, err := sandboxreader.New(namespace, objects)
	if err != nil {
		return nil, err
	}
	return &Service{
		namespace: namespace, workloadNamespaces: slices.Sorted(slices.Values(workloadNamespaces)), objects: objects,
		sandboxes: sandboxes, readers: sets.New(readers...), writers: slices.Clone(writers),
	}, nil
}

func (s *Service) Methods(readResolver, writeResolver *auth.Resolver) auth.Methods {
	rule := auth.ReviewedServiceAccount(readResolver)
	return auth.Methods{
		policypb.PolicyService_GrantSandboxPolicyBinding_FullMethodName:  auth.Accounts(writeResolver, s.writers),
		policypb.PolicyService_RevokeSandboxPolicyBinding_FullMethodName: auth.Accounts(writeResolver, s.writers),
		policypb.PolicyService_WatchPolicyBindings_FullMethodName:        rule,
		policypb.PolicyService_WatchPolicySets_FullMethodName:            rule,
		policypb.PolicyService_ListPolicySetNames_FullMethodName:         rule,
		policypb.PolicyService_WatchIdentities_FullMethodName:            rule,
		policypb.PolicyService_DeleteIdentity_FullMethodName:             auth.Accounts(writeResolver, s.writers),
		policypb.PolicyService_CreateIdentity_FullMethodName:             auth.Accounts(writeResolver, s.writers),
		policypb.PolicyService_GrantIdentityPolicyBinding_FullMethodName: auth.Accounts(writeResolver, s.writers),
	}
}

func (s *Service) caller(ctx context.Context) (auth.Account, bool, error) {
	identity, err := auth.EffectiveCaller(ctx)
	if err != nil {
		return auth.Account{}, false, err
	}
	if identity == nil || identity.GetAccount().GetNamespace() == "" || identity.GetAccount().GetName() == "" || identity.GetUid() == "" {
		return auth.Account{}, false, status.Error(codes.Unauthenticated, "incomplete caller")
	}
	caller := auth.Account{Namespace: identity.GetAccount().GetNamespace(), Name: identity.GetAccount().GetName()}
	broad := s.readers.Has(caller)
	return caller, broad, nil
}
