package gateway

import (
	"errors"
	"net/url"
	"regexp"
	"strings"

	providerpb "github.com/agentydragon/ducktape/platform_sessions/provider/providerpb"
	"google.golang.org/protobuf/types/known/timestamppb"
)

var validID = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]*$`)

func timestampValid(t *timestamppb.Timestamp) bool { return t == nil || t.CheckValid() == nil }

func upstreamStatusValid(reason int32, status *uint32, upstreamReason int32) bool {
	return status == nil || (reason == upstreamReason && *status >= 100 && *status <= 599)
}

func probeSourceValid(source providerpb.ProbeSource) bool {
	switch source {
	case providerpb.ProbeSource_PROBE_SOURCE_MANUAL, providerpb.ProbeSource_PROBE_SOURCE_SCHEDULED,
		providerpb.ProbeSource_PROBE_SOURCE_VERIFY, providerpb.ProbeSource_PROBE_SOURCE_STARTUP,
		providerpb.ProbeSource_PROBE_SOURCE_OTHER:
		return true
	default:
		return false
	}
}

func probeValid(p *providerpb.ProbeResult) bool {
	if p == nil || p.GetCompletedAt() == nil || !timestampValid(p.GetCompletedAt()) ||
		!probeSourceValid(p.GetSource()) ||
		p.GetOutcome() == nil {
		return false
	}
	switch outcome := p.GetOutcome().(type) {
	case *providerpb.ProbeResult_Verified:
		return outcome.Verified != nil
	case *providerpb.ProbeResult_ReauthenticationRequired:
		return outcome.ReauthenticationRequired != nil
	case *providerpb.ProbeResult_ChallengeRequired:
		return outcome.ChallengeRequired != nil
	case *providerpb.ProbeResult_UpstreamUnavailable:
		return outcome.UpstreamUnavailable != nil
	case *providerpb.ProbeResult_OutcomeUnknown:
		return outcome.OutcomeUnknown != nil && upstreamStatusValid(
			int32(outcome.OutcomeUnknown.GetReason()), outcome.OutcomeUnknown.UpstreamHttpStatus,
			int32(providerpb.UnknownOutcomeReason_UNKNOWN_OUTCOME_REASON_UPSTREAM_HTTP_ERROR),
		)
	case *providerpb.ProbeResult_NoSession:
		return outcome.NoSession != nil
	case *providerpb.ProbeResult_Failure:
		return outcome.Failure != nil && outcome.Failure.GetReason() != providerpb.ProbeFailureReason_PROBE_FAILURE_REASON_UNSPECIFIED
	default:
		return false
	}
}

func snapshotValid(s *providerpb.SessionSnapshot) bool {
	if s == nil || !timestampValid(s.GetLastVerifiedAt()) || !timestampValid(s.GetImportedAt()) ||
		(s.GetLastProbe() != nil && !probeValid(s.GetLastProbe())) {
		return false
	}
	switch status := s.GetStatus().(type) {
	case *providerpb.SessionSnapshot_Known:
		return status.Known != nil && status.Known.GetState() != providerpb.SessionState_SESSION_STATE_UNSPECIFIED &&
			status.Known.GetStateChangedAt() != nil && timestampValid(status.Known.GetStateChangedAt())
	case *providerpb.SessionSnapshot_StorageError:
		return status.StorageError != nil && timestampValid(status.StorageError.GetStateChangedAt())
	default:
		return false
	}
}

func importValid(r *providerpb.ImportSessionResponse) bool {
	if r == nil || r.GetOutcome() == nil {
		return false
	}
	switch outcome := r.GetOutcome().(type) {
	case *providerpb.ImportSessionResponse_Installed:
		return outcome.Installed != nil
	case *providerpb.ImportSessionResponse_Rejected:
		return outcome.Rejected != nil && outcome.Rejected.GetReason() != providerpb.CandidateRejectionReason_CANDIDATE_REJECTION_REASON_UNSPECIFIED
	case *providerpb.ImportSessionResponse_ReauthenticationRequired:
		return outcome.ReauthenticationRequired != nil
	case *providerpb.ImportSessionResponse_ChallengeRequired:
		return outcome.ChallengeRequired != nil
	case *providerpb.ImportSessionResponse_UpstreamUnavailable:
		return outcome.UpstreamUnavailable != nil
	case *providerpb.ImportSessionResponse_OutcomeUnknown:
		return outcome.OutcomeUnknown != nil && upstreamStatusValid(
			int32(outcome.OutcomeUnknown.GetReason()), outcome.OutcomeUnknown.UpstreamHttpStatus,
			int32(providerpb.UnknownOutcomeReason_UNKNOWN_OUTCOME_REASON_UPSTREAM_HTTP_ERROR),
		)
	case *providerpb.ImportSessionResponse_Failure:
		return outcome.Failure != nil && outcome.Failure.GetReason() != providerpb.ImportFailureReason_IMPORT_FAILURE_REASON_UNSPECIFIED && upstreamStatusValid(
			int32(outcome.Failure.GetReason()), outcome.Failure.UpstreamHttpStatus,
			int32(providerpb.ImportFailureReason_IMPORT_FAILURE_REASON_UPSTREAM_HTTP_ERROR),
		)
	default:
		return false
	}
}

func providerInfoValid(p *providerpb.ProviderInfo) bool {
	if p == nil || !validID.MatchString(p.GetProviderId()) || p.GetDisplayName() == "" {
		return false
	}
	seen := map[string]bool{}
	for _, i := range p.GetInstances() {
		if i == nil || !validID.MatchString(i.GetInstanceId()) || seen[i.GetInstanceId()] {
			return false
		}
		seen[i.GetInstanceId()] = true
		if i.OpenapiDocumentPath != nil {
			if _, err := segments(i.GetOpenapiDocumentPath(), true); err != nil {
				return false
			}
		}
		formats := map[string]bool{}
		for _, f := range i.GetAcceptedSessionImportFormats() {
			if f == nil || f.GetMediaType() == "" {
				return false
			}
			key := f.GetMediaType()
			if formats[key] {
				return false
			}
			formats[key] = true
		}
	}
	return true
}

func validateHTTPConfig(service *Service, config HTTPConfig) error {
	if len(config.Providers) != len(service.providers) {
		return errors.New("HTTP providers must match control providers")
	}
	for id, provider := range config.Providers {
		_, ok := service.providers[id]
		if !ok || provider.ID != id {
			return errors.New("HTTP provider identity mismatch")
		}
		if provider.OperationTimeout <= 0 || provider.OperationTimeout > maxProviderTimeout {
			return errors.New("invalid HTTP provider operation deadline")
		}
		origin, err := url.Parse(provider.Origin)
		if err != nil || origin.Scheme != "https" || origin.Host == "" || origin.User != nil || (origin.Path != "" && origin.Path != "/") || origin.RawQuery != "" || origin.Fragment != "" {
			return errors.New("invalid provider origin")
		}
		if !validCredential(provider.OwnerToken) || !validCredential(provider.RouteToken) || provider.OwnerToken == provider.RouteToken {
			return errors.New("invalid provider credentials")
		}
	}
	seen := map[string]bool{}
	for _, bearer := range config.Bearers {
		if !validCredential(bearer.Token) || seen[bearer.Token] || (bearer.Owner && len(bearer.Routes) > 0) {
			return errors.New("invalid bearer")
		}
		seen[bearer.Token] = true
		for _, route := range bearer.Routes {
			if _, ok := service.providers[route.Instance]; !ok {
				return errors.New("unknown route instance")
			}
			if route.Method == "" || strings.ToUpper(route.Method) != route.Method {
				return errors.New("invalid route method")
			}
			if _, err := segments(route.Path, true); err != nil {
				return errors.New("invalid route path")
			}
		}
	}
	return nil
}

func validCredential(value string) bool {
	if len(value) < 32 {
		return false
	}
	for _, c := range value {
		if c < 33 || c > 126 {
			return false
		}
	}
	return true
}
