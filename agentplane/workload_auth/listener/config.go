// Package listener defines the authentication methods admitted on one listener.
package listener

import (
	"errors"
	"fmt"
	"reflect"
)

type Mode string

const KubernetesBearer Mode = "kubernetes_bearer"

type Authentication interface {
	Mode() Mode
	authentication()
}

type KubernetesBearerAuth struct {
	Audience string
}

func (*KubernetesBearerAuth) authentication() {}
func (*KubernetesBearerAuth) Mode() Mode      { return KubernetesBearer }

// AuthenticationDecodeHook maps the YAML mode discriminator to the sealed
// authentication type and rejects fields that do not belong to that mode.
func AuthenticationDecodeHook(from, to reflect.Type, data any) (any, error) {
	if to != reflect.TypeOf((*Authentication)(nil)).Elem() || from == nil || from.Kind() != reflect.Map {
		return data, nil
	}
	fields, ok := data.(map[string]any)
	if !ok {
		return nil, errors.New("authentication must be a mapping with string keys")
	}
	modeValue, ok := fields["mode"]
	if !ok {
		return nil, errors.New("authentication mode is required")
	}
	modeText, ok := modeValue.(string)
	if !ok {
		return nil, errors.New("authentication mode must be a string")
	}
	switch mode := Mode(modeText); mode {
	case KubernetesBearer:
		for name := range fields {
			if name != "mode" && name != "audience" {
				return nil, fmt.Errorf("unexpected authentication field %q", name)
			}
		}
		audience, ok := fields["audience"]
		if !ok {
			return nil, errors.New(`authentication field "audience" is required`)
		}
		text, ok := audience.(string)
		if !ok || text == "" {
			return nil, errors.New(`authentication field "audience" must be a non-empty string`)
		}
		return &KubernetesBearerAuth{Audience: text}, nil
	default:
		return nil, fmt.Errorf("unknown authentication mode %q", mode)
	}
}

type Config struct {
	ListenAddress string           `mapstructure:"listen_address"`
	Auth          []Authentication `mapstructure:"auth"`
}

func (c Config) Validate() error {
	if c.ListenAddress == "" {
		return errors.New("listen_address is required")
	}
	if len(c.Auth) == 0 {
		return errors.New("auth must enable at least one mode")
	}
	seen := make(map[Mode]bool, len(c.Auth))
	for _, method := range c.Auth {
		if method == nil {
			return errors.New("authentication mode is required")
		}
		mode := method.Mode()
		if seen[mode] {
			return fmt.Errorf("duplicate authentication mode %q", mode)
		}
		seen[mode] = true
		switch method := method.(type) {
		case *KubernetesBearerAuth:
			if method == nil || method.Audience == "" {
				return errors.New("kubernetes_bearer requires audience")
			}
		default:
			return fmt.Errorf("unknown authentication mode %q", mode)
		}
	}
	return nil
}

func (c Config) BearerAudience() string {
	for _, method := range c.Auth {
		if bearer, ok := method.(*KubernetesBearerAuth); ok && bearer != nil {
			return bearer.Audience
		}
	}
	return ""
}

func (c Config) Has(mode Mode) bool {
	for _, method := range c.Auth {
		if method != nil && method.Mode() == mode {
			return true
		}
	}
	return false
}
