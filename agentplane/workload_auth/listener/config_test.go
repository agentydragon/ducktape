package listener

import "testing"

func TestBearerOnlyListenerRequiresOneConcreteModeAndAudience(t *testing.T) {
	for name, config := range map[string]Config{
		"empty":          {},
		"no auth":        {ListenAddress: ":8888"},
		"empty audience": {ListenAddress: ":8888", Auth: []Authentication{&KubernetesBearerAuth{}}},
		"nil variant":    {ListenAddress: ":8888", Auth: []Authentication{(*KubernetesBearerAuth)(nil)}},
		"duplicate":      {ListenAddress: ":8888", Auth: []Authentication{&KubernetesBearerAuth{Audience: "a"}, &KubernetesBearerAuth{Audience: "b"}}},
	} {
		t.Run(name, func(t *testing.T) {
			if err := config.Validate(); err == nil {
				t.Fatal("invalid listener configuration accepted")
			}
		})
	}
	valid := Config{ListenAddress: ":8888", Auth: []Authentication{&KubernetesBearerAuth{Audience: "test-audience"}}}
	if err := valid.Validate(); err != nil {
		t.Fatal(err)
	}
	if got := valid.BearerAudience(); got != "test-audience" {
		t.Fatalf("BearerAudience() = %q", got)
	}
}
