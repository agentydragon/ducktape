package main

import (
	"testing"
	"time"
)

func TestMatchGlob(t *testing.T) {
	tests := []struct {
		pattern string
		s       string
		want    bool
	}{
		{"*.ambr", "test_handlers/test.outputs/snapshot.ambr", true},
		{"*.ambr", "test_handlers/test.outputs/log.txt", false},
		{"*", "anything", true},
		{"*.ambr", "snapshot.ambr", true},
		{"test_*/*.ambr", "test_foo/snapshot.ambr", true},
		{"test_*/*.ambr", "other/snapshot.ambr", false},
		{"no-star", "no-star", true},
		{"no-star", "no-stardust", false},
		{"*.log", "foo/bar/test.log", true},
		{"*test.outputs/*", "foo/test.outputs/snapshot.ambr", true},
		{"test.outputs/*", "test.outputs/snapshot.ambr", true},
		{"test.outputs/*", "foo/test.outputs/snapshot.ambr", false},
		{"*.ambr", "", false},
		{"", "", true},
		{"", "nonempty", false},
	}
	for _, tt := range tests {
		got := matchGlob(tt.pattern, tt.s)
		if got != tt.want {
			t.Errorf("matchGlob(%q, %q) = %v, want %v", tt.pattern, tt.s, got, tt.want)
		}
	}
}

func TestFilterArtifacts(t *testing.T) {
	arts := []artifact{
		{Label: "//foo:test", Name: "snapshot.ambr"},
		{Label: "//foo:test", Name: "test.log"},
		{Label: "//bar:test", Name: "other.ambr"},
	}
	tests := []struct {
		pattern string
		want    int
	}{
		{".ambr", 2},
		{"*.ambr", 2},
		{"snapshot.ambr", 1},
		{"test.log", 1},
		{"nonexistent", 0},
		{"*", 3},
	}
	for _, tt := range tests {
		matches := filterArtifacts(arts, tt.pattern)
		if len(matches) != tt.want {
			t.Errorf("filterArtifacts(%q) = %d matches, want %d", tt.pattern, len(matches), tt.want)
		}
	}
}

func TestFilterToolLogs(t *testing.T) {
	logs := []toolLog{
		{InvocationID: "inv-test", Name: "elapsed time"},
		{InvocationID: "inv-test", Name: "command.profile.gz"},
		{InvocationID: "inv-build", Name: "command.profile.gz"},
	}
	tests := []struct {
		pattern string
		want    int
	}{
		{"command.profile.gz", 2},
		{"inv-test/command.profile.gz", 1},
		{"inv-*/command.profile.gz", 2},
		{"elapsed", 1},
		{"missing", 0},
	}
	for _, tt := range tests {
		matches := filterToolLogs(logs, tt.pattern)
		if len(matches) != tt.want {
			t.Errorf("filterToolLogs(%q) = %d matches, want %d", tt.pattern, len(matches), tt.want)
		}
	}
}

func TestReadToolLogInline(t *testing.T) {
	got, err := readToolLog(nil, toolLog{Name: "elapsed time", contents: []byte("12.300000")})
	if err != nil {
		t.Fatalf("readToolLog inline returned error: %v", err)
	}
	if string(got) != "12.300000" {
		t.Fatalf("readToolLog inline = %q, want %q", got, "12.300000")
	}

	_, err = readToolLog(nil, toolLog{Name: "empty"})
	if err == nil {
		t.Fatal("readToolLog empty expected error")
	}
}

func TestToolLogSourceAndSize(t *testing.T) {
	tests := []struct {
		name       string
		uri        string
		contents   []byte
		wantSource string
		wantSize   int
	}{
		{
			name:       "inline",
			contents:   []byte("critical path"),
			wantSource: "inline",
			wantSize:   13,
		},
		{
			name:       "bytestream with size",
			uri:        "bytestream://remote.buildbuddy.io/blobs/abc/3232454",
			wantSource: "bytestream",
			wantSize:   3232454,
		},
		{
			name:       "bytestream malformed size",
			uri:        "bytestream://remote.buildbuddy.io/blobs/abc/not-a-size",
			wantSource: "bytestream",
			wantSize:   0,
		},
		{
			name:       "empty",
			wantSource: "empty",
			wantSize:   0,
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			gotSource, gotSize := toolLogSourceAndSize(tt.uri, tt.contents)
			if gotSource != tt.wantSource || gotSize != tt.wantSize {
				t.Fatalf("toolLogSourceAndSize() = (%q, %d), want (%q, %d)", gotSource, gotSize, tt.wantSource, tt.wantSize)
			}
		})
	}
}

func TestNormalizeGitURL(t *testing.T) {
	tests := []struct {
		name string
		in   string
		want string
	}{
		{
			name: "ssh url with .git",
			in:   "git@github.com:user/repo.git",
			want: "https://github.com/user/repo",
		},
		{
			name: "https url with .git",
			in:   "https://github.com/user/repo.git",
			want: "https://github.com/user/repo",
		},
		{
			name: "https url without .git",
			in:   "https://github.com/user/repo",
			want: "https://github.com/user/repo",
		},
		{
			name: "ssh url without .git",
			in:   "git@github.com:user/repo",
			want: "https://github.com/user/repo",
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := normalizeGitURL(tt.in)
			if got != tt.want {
				t.Errorf("normalizeGitURL(%q) = %q, want %q", tt.in, got, tt.want)
			}
		})
	}
}

func TestParseSince(t *testing.T) {
	now := time.Date(2026, 4, 8, 12, 0, 0, 0, time.UTC)

	tests := []struct {
		name    string
		since   string
		want    time.Time
		wantErr bool
	}{
		{
			name:  "go duration 168h",
			since: "168h",
			want:  time.Date(2026, 4, 1, 12, 0, 0, 0, time.UTC),
		},
		{
			name:  "go duration 24h",
			since: "24h",
			want:  time.Date(2026, 4, 7, 12, 0, 0, 0, time.UTC),
		},
		{
			name:  "date format",
			since: "2026-04-01",
			want:  time.Date(2026, 4, 1, 0, 0, 0, 0, time.UTC),
		},
		{
			name: "empty string",
			want: time.Time{},
		},
		{
			name:    "invalid duration 7d",
			since:   "7d",
			wantErr: true,
		},
		{
			name:    "invalid string",
			since:   "invalid",
			wantErr: true,
		},
		{
			name:  "date does not match as Nd",
			since: "2026-04-08",
			want:  time.Date(2026, 4, 8, 0, 0, 0, 0, time.UTC),
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got, err := parseSince(tt.since, now)
			if tt.wantErr {
				if err == nil {
					t.Errorf("parseSince(%q) expected error, got %v", tt.since, got)
				}
				return
			}
			if err != nil {
				t.Errorf("parseSince(%q) unexpected error: %v", tt.since, err)
				return
			}
			if !got.Equal(tt.want) {
				t.Errorf("parseSince(%q) = %v, want %v", tt.since, got, tt.want)
			}
		})
	}
}
