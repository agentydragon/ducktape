package gateway

import (
	"errors"
	"net/url"
	"regexp"
	"strings"

	providerpb "github.com/agentydragon/ducktape/platform_sessions/provider/v1/providerpb"
	"google.golang.org/protobuf/types/known/timestamppb"
)

var validID = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]*$`)

func timestampValid(t *timestamppb.Timestamp) bool { return t == nil || t.CheckValid() == nil }
func probeValid(p *providerpb.ProbeResult) bool {
	if p == nil || p.GetCompletedAt() == nil || !timestampValid(p.GetCompletedAt()) || (p.GetSource() != providerpb.ProbeSource_PROBE_SOURCE_MANUAL && p.GetSource() != providerpb.ProbeSource_PROBE_SOURCE_SCHEDULED) || p.GetResult() == nil {
		return false
	}
	if f := p.GetFailure(); f != nil {
		_, ok := providerpb.ProbeFailureReason_name[int32(f.GetReason())]
		return ok && f.GetReason() != providerpb.ProbeFailureReason_PROBE_FAILURE_REASON_UNSPECIFIED
	}
	return true
}

func snapshotValid(s *providerpb.SessionSnapshot) bool {
	if s == nil {
		return false
	}
	state := s.GetState()
	_, ok := providerpb.SessionState_name[int32(state)]
	if !ok || state == providerpb.SessionState_SESSION_STATE_UNSPECIFIED || (state != providerpb.SessionState_SESSION_STATE_STORAGE_ERROR && s.GetStateChangedAt() == nil) {
		return false
	}
	return timestampValid(s.GetStateChangedAt()) && timestampValid(s.GetLastVerifiedAt()) && timestampValid(s.GetImportedAt()) && (s.GetLastProbe() == nil || probeValid(s.GetLastProbe()))
}

func rejectedValid(r *providerpb.CandidateRejected) bool {
	if r == nil {
		return true
	}
	_, ok := providerpb.CandidateRejectionReason_name[int32(r.GetReason())]
	return ok && r.GetReason() != providerpb.CandidateRejectionReason_CANDIDATE_REJECTION_REASON_UNSPECIFIED
}

func providerInfoValid(p *providerpb.ProviderInfo) bool {
	if p == nil || !validID.MatchString(p.GetProviderId()) || p.GetDisplayName() == "" || p.GetVersion() == "" {
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
			if f == nil || f.GetMediaType() == "" || f.GetVersion() == "" {
				return false
			}
			key := f.GetMediaType() + "\x00" + f.GetVersion()
			if formats[key] {
				return false
			}
			formats[key] = true
		}
		if i.GetSupportsCandidateVerification() && len(formats) == 0 {
			return false
		}
	}
	return true
}

func validateHTTPConfig(service *Service, config HTTPConfig) error {
	if len(config.Providers) != len(service.providers) || len(config.ProviderTypes) != len(service.providers) {
		return errors.New("HTTP providers must match control providers")
	}
	for id, provider := range config.Providers {
		control, ok := service.providers[id]
		if !ok || provider.ID != id || config.ProviderTypes[id] != control.Ref.ProviderId {
			return errors.New("HTTP provider identity mismatch")
		}
		origin, err := url.Parse(provider.Origin)
		if err != nil || origin.Scheme != "https" || origin.Host == "" || origin.User != nil || (origin.Path != "" && origin.Path != "/") || origin.RawQuery != "" || origin.Fragment != "" {
			return errors.New("invalid provider origin")
		}
		base, err := url.Parse(provider.Upstream)
		if err != nil || (base.Scheme != "https" && base.Scheme != "http") || base.Host == "" || base.User != nil || base.RawQuery != "" || base.Fragment != "" {
			return errors.New("invalid upstream base")
		}
		if base.EscapedPath() != "" {
			if _, err := segments(base.EscapedPath(), false); err != nil {
				return errors.New("invalid upstream base")
			}
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
