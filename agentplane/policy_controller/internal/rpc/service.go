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
	readerServiceIDs   []string
	readerServices     sets.Set[string]
	writerServices     []string
}

func New(
	namespace string,
	workloadNamespaces []string,
	objects client.WithWatch,
	readerServices, writerServices []string,
) (*Service, error) {
	if namespace == "" || objects == nil {
		return nil, errors.New("policy namespace and Kubernetes watch client are required")
	}
	if err := auth.ValidateServiceCallers(readerServices); err != nil {
		return nil, errors.New("invalid PolicyService reader service allowlist")
	}
	if err := auth.ValidateServiceCallers(writerServices); err != nil {
		return nil, errors.New("invalid PolicyService writer service allowlist")
	}
	sandboxes, err := sandboxreader.New(namespace, objects)
	if err != nil {
		return nil, err
	}
	return &Service{
		namespace: namespace, workloadNamespaces: slices.Sorted(slices.Values(workloadNamespaces)), objects: objects,
		sandboxes:        sandboxes,
		readerServiceIDs: slices.Clone(readerServices), readerServices: sets.New(readerServices...), writerServices: slices.Clone(writerServices),
	}, nil
}

var (
	readerServiceMethods = []string{
		policypb.PolicyService_WatchPolicyBindings_FullMethodName,
		policypb.PolicyService_WatchPolicySets_FullMethodName,
		policypb.PolicyService_ListPolicySetNames_FullMethodName,
		policypb.PolicyService_WatchIdentities_FullMethodName,
	}
	writerServiceMethods = []string{
		policypb.PolicyService_GrantSandboxPolicyBinding_FullMethodName,
		policypb.PolicyService_RevokeSandboxPolicyBinding_FullMethodName,
		policypb.PolicyService_DeleteIdentity_FullMethodName,
		policypb.PolicyService_CreateIdentity_FullMethodName,
		policypb.PolicyService_GrantIdentityPolicyBinding_FullMethodName,
	}
)

func (s *Service) Methods() (auth.Methods, error) {
	reader, err := auth.Services(s.readerServiceIDs)
	if err != nil {
		return nil, err
	}
	writer, err := auth.Services(s.writerServices)
	if err != nil {
		return nil, err
	}
	methods := auth.Methods{}
	for _, method := range readerServiceMethods {
		methods[method] = reader
	}
	for _, method := range writerServiceMethods {
		methods[method] = writer
	}
	return methods, nil
}

func (s *Service) requireReader(ctx context.Context) error {
	admitted, err := auth.Admitted(ctx)
	if err != nil {
		return err
	}
	service, ok := admitted.(auth.ServiceAdmission)
	if !ok {
		return status.Error(codes.Unauthenticated, "service caller required")
	}
	if !s.readerServices.Has(service.Caller.SPIFFEID) {
		return status.Error(codes.PermissionDenied, "service cannot read policy resources")
	}
	return nil
}
