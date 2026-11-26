package settings

import "github.com/agentydragon/ducktape/agentplane/eventblock"

// PackingProfile fills omitted Sampler settings with its 128-entry default.
func (s Settings) PackingProfile() eventblock.Profile {
	p := s.EventBlockProfile
	defaults := eventblock.DefaultProfile(128)
	if p.TargetBytes == 0 {
		p.TargetBytes = defaults.TargetBytes
	}
	if p.TargetEntries == 0 {
		p.TargetEntries = defaults.TargetEntries
	}
	if p.Level == 0 {
		p.Level = defaults.Level
	}
	if p.Codec == "" {
		p.Codec = defaults.Codec
	}
	return p
}
