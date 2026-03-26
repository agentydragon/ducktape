// Reconstructed from binary: environment-manager (Build ID 64bc4dc1)
// Source: internal/tunnel/actions/snapshot/action.go
// Module: github.com/anthropics/anthropic/api-go/environment-manager
//
// Package snapshot implements the snapshot tunnel action.

package snapshot

import (
	"context"
	"fmt"
	"log/slog"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"github.com/anthropics/anthropic/api-go/environment-manager/internal/tunnel/actions"
)

// fileEntry represents a file in the snapshot directory listing.
//
// Binary type: *snapshot.fileEntry
type fileEntry struct {
	Name    string `json:"name"`
	Size    int64  `json:"size"`
	IsDir   bool   `json:"is_dir"`
	ModTime string `json:"mod_time,omitempty"`
}

// snapshotResponse is the JSON response returned by the snapshot action.
//
// Binary type: *snapshot.snapshotResponse
type snapshotResponse struct {
	ProjectFiles []fileEntry  `json:"project_files"` // offset 0x00: files from project dir
	HomeFiles    []fileEntry  `json:"home_files"`    // files from home dir
	TotalFiles   int          `json:"total_files"`   // total count from both dirs
	HasGitRepo   bool         `json:"has_git_repo"`  // whether project has .git
	GitModified  bool         `json:"git_modified"`  // whether git shows modifications
	HasCommits   bool         `json:"has_commits"`   // commitCount > 1
	CommitCount  int64        `json:"commit_count"`  // number of git commits
	Truncated    bool         `json:"truncated"`     // whether file listing was truncated
	Logger       *slog.Logger `json:"-"`             // offset 0x00 of SnapshotAction
}

// SnapshotAction implements the actions.Action interface for project snapshots.
// It reads directory listings and git status to build a snapshot of the project state.
//
// Struct layout (from DWARF):
//
//	offset 0x00: *slog.Logger
//	offset 0x08: projectDir string (ptr)
//	offset 0x10: projectDir string (len)
//	offset 0x18: homeDir string (ptr)
//	offset 0x20: homeDir string (len)
//	offset 0x28: workDir string (ptr)
//	offset 0x30: workDir string (len)
//
// Binary type: *snapshot.SnapshotAction
type SnapshotAction struct {
	Logger     *slog.Logger // offset 0x00
	ProjectDir string       // offset 0x08
	HomeDir    string       // offset 0x18
	WorkDir    string       // offset 0x28
}

// NewSnapshotAction creates a new SnapshotAction.
func NewSnapshotAction(logger *slog.Logger, projectDir, homeDir, workDir string) *SnapshotAction {
	return &SnapshotAction{
		Logger:     logger,
		ProjectDir: projectDir,
		HomeDir:    homeDir,
		WorkDir:    workDir,
	}
}

// Name returns "snapshot" (8 chars).
//
// Binary: 0xba1a20 - (*SnapshotAction).Name
func (a *SnapshotAction) Name() string {
	return "snapshot"
}

// Timeout returns 30 seconds.
//
// Binary: 0xba1a40 - (*SnapshotAction).Timeout
// 0x6fc23ac00 = 30,000,000,000 ns = 30s
func (a *SnapshotAction) Timeout() time.Duration {
	return 30 * time.Second
}

// Execute performs the snapshot action. It evaluates symlinks on the project
// and home directories, reads directory listings from both, counts git commits,
// and checks if git shows app modifications.
//
// Binary: 0xba1a60 - (*SnapshotAction).Execute
// Source: action.go:88
//
// Flow:
//  1. EvalSymlinks on projectDir (action.go:96)
//  2. If error, return fmt.Errorf with path and error (action.go:97-98)
//  3. readDir on projectDir (action.go:104)
//  4. readDir on homeDir (action.go:108)
//  5. gitCommitCount (action.go:112)
//  6. gitAppModified (action.go:113)
//  7. Compute totalFiles = projectFileCount + homeFileCount (action.go:109)
//  8. Build snapshotResponse with all fields (action.go:116-131)
//  9. Return ActionResult with response
func (a *SnapshotAction) Execute(ctx context.Context, path string, body []byte, reporter actions.ProgressReporter) (*actions.ActionResult, error) {
	// action.go:96 - Resolve symlinks on project directory
	resolvedProjectDir, err := filepath.EvalSymlinks(a.WorkDir)
	if err != nil {
		// action.go:97-98 - Return error with path info
		return nil, fmt.Errorf("failed to resolve project path %s: %w", a.WorkDir, err)
	}

	// action.go:104 - Read project directory listing
	projectFiles, projectCount, projectTruncated := a.readDir(ctx, a.ProjectDir, a.HomeDir, resolvedProjectDir, "", false)

	// action.go:108 - Read home directory listing
	homeFiles, homeCount, homeTruncated := a.readDir(ctx, a.HomeDir, a.HomeDir, resolvedProjectDir, "", false)

	// action.go:112 - Count git commits
	commitCount := a.gitCommitCount(ctx)

	// action.go:113 - Check if git shows app modifications
	gitModified := a.gitAppModified(ctx, commitCount)

	// action.go:109 - Total file count from both directories
	totalFiles := projectCount + homeCount

	// action.go:110 - Combine truncation flags
	truncated := projectTruncated || homeTruncated

	// action.go:116-131 - Build response
	resp := &snapshotResponse{
		ProjectFiles: projectFiles,
		HomeFiles:    homeFiles,
		TotalFiles:   totalFiles,
		HasCommits:   commitCount > 1, // action.go:118
		CommitCount:  commitCount,     // action.go:119
		Truncated:    truncated,       // action.go:120
		GitModified:  gitModified,     // action.go:121
	}

	// action.go:124 - Log result
	a.Logger.Info("Snapshot complete",
		"request_id", path,
	)

	return &actions.ActionResult{Data: resp}, nil
}

// readDir reads a directory listing, resolving symlinks and checking for
// path escapes. Returns the file entries, total count, and whether the
// listing was truncated.
//
// Binary: 0xba20c0 - (*SnapshotAction).readDir
// Source: action.go:145
//
// Flow:
//  1. EvalSymlinks on dirPath (action.go:153)
//  2. If error is os.ErrNotExist, log warning and return empty (action.go:156-159)
//  3. If other error, log warning with 4 attrs and return empty (action.go:157)
//  4. ReadDir on resolved path
//  5. For each entry, build fileEntry with name, size, isDir
//  6. Check for path escapes via filepath.Rel
func (a *SnapshotAction) readDir(
	ctx context.Context,
	dirPath string,
	homeDir string,
	projectDir string,
	prefix string,
	recursive bool,
) ([]fileEntry, int, bool) {
	// action.go:153 - Resolve symlinks
	resolved, err := filepath.EvalSymlinks(dirPath)
	if err != nil {
		// action.go:156 - Check for not-exist
		if os.IsNotExist(err) {
			// action.go:159 - Return empty result
			return nil, 0, false
		}
		// action.go:157 - Log warning and return empty
		a.Logger.Warn("failed to resolve directory path",
			"path", dirPath,
			"error", err,
		)
		return nil, 0, false
	}

	// Read directory entries
	entries, err := os.ReadDir(resolved)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, 0, false
		}
		a.Logger.Warn("failed to read directory",
			"path", resolved,
			"error", err,
		)
		return nil, 0, false
	}

	var files []fileEntry
	count := 0
	truncated := false

	for _, entry := range entries {
		info, err := entry.Info()
		if err != nil {
			continue
		}

		fe := fileEntry{
			Name:  filepath.Join(prefix, entry.Name()),
			IsDir: entry.IsDir(),
			Size:  info.Size(),
		}
		files = append(files, fe)
		count++
	}

	return files, count, truncated
}

// readFileSafe safely reads a file, handling errors gracefully.
// Opens the file with O_RDONLY, checks for EACCES and ErrNotExist errors.
//
// Binary: 0xba2880 - (*SnapshotAction).readFileSafe
// Source: action.go:206
//
// Flow:
//  1. filepath.Join(dir, filename) (action.go:210)
//  2. os.OpenFile(path, O_RDONLY, 0) (action.go:214)
//  3. Check for EACCES or ErrNotExist errors (action.go:218)
//  4. If error, log warning and return empty (action.go:219)
//  5. Read file contents
//  6. Return contents
func (a *SnapshotAction) readFileSafe(dir string, filename string) (string, bool) {
	path := filepath.Join(dir, filename)

	// action.go:214 - Open file read-only
	f, err := os.OpenFile(path, os.O_RDONLY, 0)
	if err != nil {
		// action.go:218 - Check for permission denied or not exist
		if os.IsPermission(err) || os.IsNotExist(err) {
			return "", false
		}
		a.Logger.Warn("failed to read file",
			"path", path,
			"error", err,
		)
		return "", false
	}
	defer f.Close()

	data, err := os.ReadFile(path)
	if err != nil {
		return "", false
	}

	return string(data), true
}

// gitCommitCount returns the number of git commits in the working directory.
// Runs: git -C <workDir> rev-list --count HEAD
//
// Binary: 0xba3120 - (*SnapshotAction).gitCommitCount
// Source: action.go:252
//
// Flow:
//  1. Build command args: ["git", "-C", workDir, "rev-list", "--count", "HEAD"]
//     (action.go:255, string lengths: "git"=3, "-C"=2, "rev-list"=8, "--count"=7, "HEAD"=4)
//  2. exec.CommandContext(ctx, "git", args...).Output() (action.go:255-256)
//  3. If error, log warning and return 0 (action.go:257-258)
//  4. Parse output as int64 (action.go:260+)
//  5. Return count
func (a *SnapshotAction) gitCommitCount(ctx context.Context) int64 {
	// action.go:255 - Run git rev-list --count HEAD
	output, err := exec.CommandContext(ctx, "git", "-C", a.WorkDir, "rev-list", "--count", "HEAD").Output()
	if err != nil {
		// action.go:257-258 - Log and return 0
		a.Logger.Warn("failed to get git commit count",
			"work_dir", a.WorkDir,
			"error", err,
		)
		return 0
	}

	// Parse the count
	countStr := strings.TrimSpace(string(output))
	count, err := strconv.ParseInt(countStr, 10, 64)
	if err != nil {
		return 0
	}

	return count
}

// gitAppModified checks if git shows modifications (uncommitted changes).
// Only runs if commitCount > 1.
// Runs: git -C <workDir> diff --name-only --diff-filter=M HEAD
//
// Binary: 0xba33a0 - (*SnapshotAction).gitAppModified
// Source: action.go:276
//
// Flow:
//  1. If commitCount <= 1, return false (action.go:277)
//  2. Build command: git -C <workDir> diff --name-only --diff-filter=M HEAD
//     (action.go:283, string lengths: "git"=3, "-C"=2, "diff"=8, "--name-only"=15, "--diff-filter=M"=4... wait no)
//     Actually from disasm: args are 5 strings of length 2, workDir, 8, 15, 4
//     -> "git"(3), "-C"(2), workDir, "diff"(8??)
//     Looking at lengths: 0x02="--", 0x08="rev-list"?, 0x0f=15="--diff-filter=M"? 0x04="HEAD"
//     Re-reading: cmd args = ["-C", workDir, "diff", "--name-only", "HEAD"]
//  3. exec.CommandContext(ctx, "git", args...).Output() (action.go:283-284)
//  4. If error, log warning and return false (action.go:285-286)
//  5. Check if output is non-empty (has modified files)
//  6. Return true if modified
func (a *SnapshotAction) gitAppModified(ctx context.Context, commitCount int64) bool {
	// action.go:277 - Skip if no commits
	if commitCount <= 1 {
		return false
	}

	// action.go:283 - Run git diff --name-only
	output, err := exec.CommandContext(ctx, "git", "-C", a.WorkDir, "diff", "--name-only", "HEAD").Output()
	if err != nil {
		// action.go:285-286 - Log and return false
		a.Logger.Warn("failed to check git modifications",
			"work_dir", a.WorkDir,
			"error", err,
		)
		return false
	}

	// Check if there are any modified files
	return len(strings.TrimSpace(string(output))) > 0
}
