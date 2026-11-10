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
	if p == nil || p.GetCompletedAt() == nil || !timestampValid(p.GetCompletedAt()) ||
		(p.GetSource() != providerpb.ProbeSource_PROBE_SOURCE_MANUAL && p.GetSource() != providerpb.ProbeSource_PROBE_SOURCE_SCHEDULED) ||
		p.GetOutcome() == providerpb.ProbeOutcome_PROBE_OUTCOME_UNSPECIFIED {
		return false
	}
	if p.GetOutcome() == providerpb.ProbeOutcome_PROBE_OUTCOME_UNKNOWN {
		return p.GetOutcomeUnknown() != nil
	}
	if p.GetOutcome() == providerpb.ProbeOutcome_PROBE_OUTCOME_FAILURE {
		return p.GetFailure() != nil && p.GetFailure().GetReason() != providerpb.ProbeFailureReason_PROBE_FAILURE_REASON_UNSPECIFIED
	}
	return true
}

func snapshotValid(s *providerpb.SessionSnapshot) bool {
	return s != nil && s.GetState() != providerpb.SessionState_SESSION_STATE_UNSPECIFIED &&
		timestampValid(s.GetStateChangedAt()) && timestampValid(s.GetLastVerifiedAt()) && timestampValid(s.GetImportedAt()) &&
		(s.GetLastProbe() == nil || probeValid(s.GetLastProbe()))
}

func importValid(r *providerpb.ImportSessionResponse) bool {
	switch r.GetOutcome() {
	case providerpb.ImportOutcome_IMPORT_OUTCOME_UNSPECIFIED:
		return false
	case providerpb.ImportOutcome_IMPORT_OUTCOME_UNKNOWN:
		return r.GetOutcomeUnknown() != nil
	case providerpb.ImportOutcome_IMPORT_OUTCOME_REJECTED:
		return r.GetRejected() != nil && r.GetRejected().GetReason() != providerpb.CandidateRejectionReason_CANDIDATE_REJECTION_REASON_UNSPECIFIED
	case providerpb.ImportOutcome_IMPORT_OUTCOME_FAILURE:
		return r.GetFailure() != nil && r.GetFailure().GetReason() != providerpb.ImportFailureReason_IMPORT_FAILURE_REASON_UNSPECIFIED
	default:
		return true
	}
}

func verifyValid(r *providerpb.VerifyCandidateResponse) bool {
	switch r.GetOutcome() {
	case providerpb.VerifyOutcome_VERIFY_OUTCOME_UNSPECIFIED:
		return false
	case providerpb.VerifyOutcome_VERIFY_OUTCOME_UNKNOWN:
		return r.GetOutcomeUnknown() != nil
	case providerpb.VerifyOutcome_VERIFY_OUTCOME_REJECTED:
		return r.GetRejected() != nil && r.GetRejected().GetReason() != providerpb.CandidateRejectionReason_CANDIDATE_REJECTION_REASON_UNSPECIFIED
	default:
		return true
	}
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
	if len(config.Providers) != len(service.providers) {
		return errors.New("HTTP providers must match control providers")
	}
	for id, provider := range config.Providers {
		_, ok := service.providers[id]
		if !ok || provider.ID != id {
			return errors.New("HTTP provider identity mismatch")
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
