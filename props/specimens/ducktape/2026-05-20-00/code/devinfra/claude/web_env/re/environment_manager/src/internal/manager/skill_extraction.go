// Reconstructed from a6f96673 DWARF extraction, carried forward to 495ea204.
// Source: internal/manager/skill_extraction.go
//
// Original source path:
//   /home/runner/work/anthropic/anthropic/api-go/environment-manager/internal/manager/skill_extraction.go
//
// Key symbols:
//   - manager.extractSkillZips (0xbaa6a0)
//   - manager.extractZip (0xbaafc0)
//   - manager.extractZipEntry (0xbab480)
//   - manager.sanitizeZipEntryPath (0xbabec0)
//   - manager.writeZipEntry (referenced in extractZipEntry)
//
// This file handles extraction of skill ZIP archives from a skills directory
// into their target locations. Used during environment setup to deploy
// pre-packaged Claude Code skills.

package manager

import (
	"archive/zip"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
)

// extractSkillZips reads ZIP files from the skills source directory and
// extracts each one into the target directory.
//
// Binary address: 0xbaa6a0
// Source lines: 39-94
//
// Assembly flow:
//  1. os.ReadDir(srcDir) at line 40 (srcDir string len 0x0b = "/mnt/skills")
//  2. If ReadDir error (line 41):
//     a. Check errors.Is(err, os.ErrNotExist) at line 42
//     b. If not exist: log warning, return empty results at line 43-44
//     c. Otherwise: return wrapped error at line 48-49
//  3. Iterate directory entries at line 55
//  4. For each entry: check if it ends with ".zip" at line 56-57
//  5. Build full path: filepath.Join(srcDir, entry.Name()) at line 59
//  6. Call extractZip(zipPath, destDir) at line 62
//  7. Collect results (extracted skill names) at lines 63-94
func extractSkillZips(srcDir string, destDir string, logger *slog.Logger) ([]string, error) {
	entries, err := os.ReadDir(srcDir)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			logger.Warn("skills directory does not exist, skipping extraction",
				"src_dir", srcDir,
			)
			return nil, nil
		}
		return nil, fmt.Errorf("failed to read skills directory %s: %w", srcDir, err)
	}

	var extracted []string
	for _, entry := range entries {
		name := entry.Name()
		if !strings.HasSuffix(name, ".zip") {
			continue
		}

		zipPath := filepath.Join(srcDir, name)
		skillName := strings.TrimSuffix(name, ".zip")
		destPath := filepath.Join(destDir, skillName)

		logger.Info("extracting skill zip",
			"zip_path", zipPath,
			"dest_path", destPath,
			"skill_name", skillName,
		)

		if err := extractZip(zipPath, destPath, logger); err != nil {
			logger.Error("failed to extract skill zip",
				"zip_path", zipPath,
				"error", err,
			)
			return extracted, fmt.Errorf("failed to extract skill %s: %w", skillName, err)
		}

		extracted = append(extracted, skillName)
	}

	return extracted, nil
}

// extractZip opens a ZIP file and extracts all entries to the destination directory.
//
// Binary address: 0xbaafc0
// Source lines: 101-136
//
// Assembly flow:
//  1. zip.OpenReader(zipPath) at line 103
//  2. If error: wrap and return at line 104
//  3. defer reader.Close() at line 106
//  4. Iterate reader.File entries at line 109
//  5. For each entry: call extractZipEntry at line 113
//  6. On entry error: return wrapped error at line 114-116
func extractZip(zipPath string, destDir string, logger *slog.Logger) error {
	reader, err := zip.OpenReader(zipPath)
	if err != nil {
		return fmt.Errorf("failed to open zip %s: %w", zipPath, err)
	}
	defer reader.Close()

	for _, file := range reader.File {
		if err := extractZipEntry(file, destDir, logger); err != nil {
			return fmt.Errorf("failed to extract entry %s: %w", file.Name, err)
		}
	}

	return nil
}

// extractZipEntry extracts a single ZIP entry to the destination directory.
// It sanitizes the entry path to prevent path traversal attacks, creates
// directories as needed, and writes file content.
//
// Binary address: 0xbab480
// Source lines: 140-209
//
// Assembly flow:
//  1. Call sanitizeZipEntryPath at line 142
//  2. If sanitization error: return at line 143
//  3. Build destPath = filepath.Join(destDir, sanitizedName) at line 147
//  4. If entry is directory: os.MkdirAll at line 148-149
//  5. If entry is file: create parent dir, write file at lines 152-209
func extractZipEntry(file *zip.File, destDir string, logger *slog.Logger) error {
	sanitizedName, err := sanitizeZipEntryPath(file.Name, destDir)
	if err != nil {
		return err
	}

	destPath := filepath.Join(destDir, sanitizedName)

	if file.FileInfo().IsDir() {
		return os.MkdirAll(destPath, 0o755)
	}

	// Create parent directory
	if err := os.MkdirAll(filepath.Dir(destPath), 0o755); err != nil {
		return fmt.Errorf("creating destination %s: %w", filepath.Dir(destPath), err)
	}

	return writeZipEntry(file, destPath)
}

// writeZipEntry writes a single ZIP file entry to disk.
//
// Source lines: 160-209
func writeZipEntry(file *zip.File, destPath string) error {
	src, err := file.Open()
	if err != nil {
		return fmt.Errorf("failed to open zip entry %s: %w", file.Name, err)
	}
	defer src.Close()

	dst, err := os.OpenFile(destPath, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, file.Mode())
	if err != nil {
		return fmt.Errorf("error creating file %s: %w", destPath, err)
	}
	defer dst.Close()

	if _, err := io.Copy(dst, src); err != nil {
		return fmt.Errorf("failed to copy %s to %s: %w", file.Name, destPath, err)
	}

	return nil
}

// sanitizeZipEntryPath validates and cleans a ZIP entry path to prevent
// path traversal (zip slip) attacks. Returns the cleaned path or an error
// if the path escapes the destination directory.
//
// Binary address: 0xbabec0
// Source lines: 214-223
//
// Assembly flow:
//  1. filepath.Join(destDir, name) at line 215-216
//  2. filepath.Rel(destDir, joined) to check if path escapes at line 217
//  3. If relative path starts with ".." : return error at line 219-220
//  4. Return cleaned name at line 223
func sanitizeZipEntryPath(name string, destDir string) (string, error) {
	cleanName := filepath.Clean(name)
	destPath := filepath.Join(destDir, cleanName)

	// Ensure the resolved path is within destDir
	rel, err := filepath.Rel(destDir, destPath)
	if err != nil {
		return "", fmt.Errorf("failed to resolve relative path for %s: %w", name, err)
	}

	if strings.HasPrefix(rel, "..") {
		return "", fmt.Errorf("zip: insecure file path")
	}

	return cleanName, nil
}
