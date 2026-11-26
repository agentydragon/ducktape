package settings

import "github.com/agentydragon/ducktape/agentplane/eventblock"

const (
	defaultEventBlockTargetBytes   = 64 << 10
	defaultEventBlockTargetEntries = 128
	defaultEventBlockLevel         = 3
)

// EventBlockProfile resolves omitted JSON fields to the sampler's stock profile.
func (s Settings) EventBlockProfile() eventblock.Profile {
	p := eventblock.Profile{
		TargetBytes:   s.EventBlockTargetBytes,
		TargetEntries: s.EventBlockTargetEntries,
		Level:         s.EventBlockLevel,
	}
	if p.TargetBytes == 0 {
		p.TargetBytes = defaultEventBlockTargetBytes
	}
	if p.TargetEntries == 0 {
		p.TargetEntries = defaultEventBlockTargetEntries
	}
	if p.Level == 0 {
		p.Level = defaultEventBlockLevel
	}
	return p
}
