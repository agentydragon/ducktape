package listener

import "testing"

func TestSelectionIsAnAllowlistNotFallbackOrder(t *testing.T) {
	for _, methods := range [][]Authentication{
		{&SPIFFEAuth{CAFile: "/roots.pem"}, &KubernetesBearerAuth{Audience: "test-audience"}},
		{&KubernetesBearerAuth{Audience: "test-audience"}, &SPIFFEAuth{CAFile: "/roots.pem"}},
	} {
		config := Config{ListenAddress: ":443", TLS: &TLS{CertFile: "/cert.pem", KeyFile: "/key.pem"}, Auth: methods}
		if err := config.Validate(); err != nil {
			t.Fatal(err)
		}
		for _, test := range []struct {
			certificate, bearer bool
			want                Mode
		}{
			{certificate: true, want: SPIFFE},
			{bearer: true, want: KubernetesBearer},
			{},
			{certificate: true, bearer: true},
		} {
			mode, err := config.Select(test.certificate, test.bearer)
			if (err != nil) != (test.want == "") || mode != test.want {
				t.Fatalf("Select(%v, %v) = %q, %v; want %q", test.certificate, test.bearer, mode, err, test.want)
			}
		}
	}
	for _, mode := range []Mode{SPIFFE, KubernetesBearer} {
		var method Authentication = &SPIFFEAuth{CAFile: "/roots.pem"}
		if mode == KubernetesBearer {
			method = &KubernetesBearerAuth{Audience: "test-audience"}
		}
		config := Config{Auth: []Authentication{method}}
		if _, err := config.Select(mode != SPIFFE, mode == SPIFFE); err == nil {
			t.Fatalf("disabled mode accepted with only %q enabled", mode)
		}
	}
}

func TestInvalidAuthenticationConfigurationIsRefused(t *testing.T) {
	for name, config := range map[string]Config{
		"empty":                   {},
		"no auth":                 {ListenAddress: ":443"},
		"nil auth variant":        {ListenAddress: ":443", Auth: []Authentication{(*SPIFFEAuth)(nil)}},
		"duplicate":               {ListenAddress: ":443", Auth: []Authentication{&KubernetesBearerAuth{Audience: "a"}, &KubernetesBearerAuth{Audience: "b"}}},
		"bearer without audience": {ListenAddress: ":443", Auth: []Authentication{&KubernetesBearerAuth{}}},
		"spiffe without tls":      {ListenAddress: ":443", Auth: []Authentication{&SPIFFEAuth{CAFile: "/roots"}}},
		"spiffe relative roots":   {ListenAddress: ":443", TLS: &TLS{CertFile: "/cert", KeyFile: "/key"}, Auth: []Authentication{&SPIFFEAuth{CAFile: "roots"}}},
		"relative serving key":    {ListenAddress: ":443", TLS: &TLS{CertFile: "/cert", KeyFile: "key"}, Auth: []Authentication{&KubernetesBearerAuth{Audience: "a"}}},
	} {
		t.Run(name, func(t *testing.T) {
			if err := config.Validate(); err == nil {
				t.Fatal("invalid configuration accepted")
			}
		})
	}
}
