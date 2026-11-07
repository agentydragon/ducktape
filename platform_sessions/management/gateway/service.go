// Package gateway implements management of independently owned session providers.
package gateway

import (
	"context"
	"errors"
	"sort"
	"sync"

	managementpb "github.com/agentydragon/ducktape/platform_sessions/management/v1/managementpb"
	providerpb "github.com/agentydragon/ducktape/platform_sessions/provider/v1/providerpb"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/emptypb"
)

const maxSessionBytes = 65536

type Provider struct {
	Ref    *providerpb.InstanceRef
	Client providerpb.SessionProviderControlClient
}

// Service resolves an instance to its remote owner. Providers remain the authority
// for session state and the typed results returned to the caller.
type Service struct {
	managementpb.UnimplementedSessionManagementServer
	providers map[string]Provider
}

func New(providers []Provider) (*Service, error) {
	service := &Service{providers: make(map[string]Provider, len(providers))}
	for _, p := range providers {
		if p.Ref == nil || !validID.MatchString(p.Ref.ProviderId) || !validID.MatchString(p.Ref.InstanceId) || p.Client == nil {
			return nil, errors.New("provider identity and client are required")
		}
		if _, ok := service.providers[p.Ref.InstanceId]; ok {
			return nil, errors.New("duplicate provider instance")
		}
		service.providers[p.Ref.InstanceId] = p
	}
	return service, nil
}

func (s *Service) provider(instance string) (Provider, error) {
	p, ok := s.providers[instance]
	if !ok {
		return Provider{}, status.Error(codes.NotFound, "unknown provider instance")
	}
	return p, nil
}

func (s *Service) ListInstances(ctx context.Context, _ *emptypb.Empty) (*managementpb.ListInstancesResponse, error) {
	result := &managementpb.ListInstancesResponse{Instances: make([]*managementpb.Instance, 0, len(s.providers))}
	var mu sync.Mutex
	var wg sync.WaitGroup
	for _, p := range s.providers {
		wg.Add(1)
		go func(p Provider) {
			defer wg.Done()
			item := &managementpb.Instance{Ref: p.Ref}
			if info, err := p.Client.GetInfo(ctx, &providerpb.GetInfoRequest{}, grpc.WaitForReady(false)); err == nil && info.GetProvider().GetProviderId() == p.Ref.ProviderId && providerInfoValid(info.GetProvider()) {
				for _, instance := range info.GetProvider().GetInstances() {
					if instance.GetInstanceId() == p.Ref.InstanceId {
						item.Info = instance
						break
					}
				}
			}
			mu.Lock()
			result.Instances = append(result.Instances, item)
			mu.Unlock()
		}(p)
	}
	wg.Wait()
	sort.Slice(result.Instances, func(i, j int) bool {
		return result.Instances[i].GetRef().GetInstanceId() < result.Instances[j].GetRef().GetInstanceId()
	})
	return result, nil
}

func sameRef(a, b *providerpb.InstanceRef) bool {
	return a != nil && b != nil && a.ProviderId == b.ProviderId && a.InstanceId == b.InstanceId
}

func (s *Service) GetSession(ctx context.Context, req *managementpb.InstanceRequest) (*providerpb.GetSessionResponse, error) {
	p, err := s.provider(req.GetInstanceId())
	if err != nil {
		return nil, err
	}
	response, err := p.Client.GetSession(ctx, &providerpb.GetSessionRequest{Instance: p.Ref}, grpc.WaitForReady(false))
	if err != nil || response == nil || !sameRef(response.GetInstance(), p.Ref) || !snapshotValid(response.GetSnapshot()) {
		return nil, status.Error(codes.Unavailable, "provider service unavailable")
	}
	return response, nil
}

func (s *Service) Probe(ctx context.Context, req *managementpb.InstanceRequest) (*providerpb.ProbeResponse, error) {
	p, err := s.provider(req.GetInstanceId())
	if err != nil {
		return nil, err
	}
	response, err := p.Client.Probe(ctx, &providerpb.ProbeRequest{Instance: p.Ref, Source: providerpb.ProbeSource_PROBE_SOURCE_MANUAL}, grpc.WaitForReady(false))
	if err != nil || response == nil || !sameRef(response.GetInstance(), p.Ref) || !probeValid(response.GetResult()) || response.GetResult().GetSource() != providerpb.ProbeSource_PROBE_SOURCE_MANUAL {
		return nil, status.Error(codes.Unknown, "probe outcome unknown; inspect session state before retrying")
	}
	return response, nil
}

func candidateValid(req *managementpb.CandidateRequest) bool {
	return len(req.GetCandidate()) > 0 && len(req.GetCandidate()) <= maxSessionBytes && req.GetMediaType() != "" && req.GetFormatVersion() != ""
}

func (s *Service) ImportSession(ctx context.Context, req *managementpb.CandidateRequest) (*providerpb.ImportSessionResponse, error) {
	p, err := s.provider(req.GetInstanceId())
	if err != nil {
		return nil, err
	}
	if !candidateValid(req) {
		return nil, status.Error(codes.InvalidArgument, "invalid session candidate")
	}
	response, err := p.Client.ImportSession(ctx, &providerpb.ImportSessionRequest{Instance: p.Ref, MediaType: req.GetMediaType(), FormatVersion: req.GetFormatVersion(), Candidate: req.GetCandidate()}, grpc.WaitForReady(false))
	if err != nil || response == nil || !sameRef(response.GetInstance(), p.Ref) || response.GetResult() == nil || !rejectedValid(response.GetRejected()) {
		return nil, status.Error(codes.Unknown, "operation outcome unknown; inspect session state before retrying")
	}
	return response, nil
}

func (s *Service) VerifyCandidate(ctx context.Context, req *managementpb.CandidateRequest) (*providerpb.VerifyCandidateResponse, error) {
	p, err := s.provider(req.GetInstanceId())
	if err != nil {
		return nil, err
	}
	if !candidateValid(req) {
		return nil, status.Error(codes.InvalidArgument, "invalid session candidate")
	}
	response, err := p.Client.VerifyCandidate(ctx, &providerpb.VerifyCandidateRequest{Instance: p.Ref, MediaType: req.GetMediaType(), FormatVersion: req.GetFormatVersion(), Candidate: req.GetCandidate()}, grpc.WaitForReady(false))
	if status.Code(err) == codes.Unimplemented {
		return nil, status.Error(codes.Unimplemented, "candidate verification unsupported")
	}
	if err != nil || response == nil || !sameRef(response.GetInstance(), p.Ref) || response.GetResult() == nil || !rejectedValid(response.GetRejected()) {
		return nil, status.Error(codes.Unknown, "operation outcome unknown; inspect session state before retrying")
	}
	return response, nil
}
