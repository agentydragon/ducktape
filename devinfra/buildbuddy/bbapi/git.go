package main

import (
	"fmt"

	git "github.com/go-git/go-git/v5"
)

// detectRepoURL reads the git remote URL from the current directory.
// Prefers "origin", falls back to the first remote found.
func detectRepoURL() (string, error) {
	repo, err := git.PlainOpenWithOptions(".", &git.PlainOpenOptions{DetectDotGit: true})
	if err != nil {
		return "", fmt.Errorf("open git repo: %w", err)
	}
	remotes, err := repo.Remotes()
	if err != nil {
		return "", fmt.Errorf("list remotes: %w", err)
	}
	if len(remotes) == 0 {
		return "", fmt.Errorf("no git remotes found")
	}
	for _, r := range remotes {
		if r.Config().Name == "origin" {
			if urls := r.Config().URLs; len(urls) > 0 {
				return normalizeGitURL(urls[0]), nil
			}
		}
	}
	if urls := remotes[0].Config().URLs; len(urls) > 0 {
		return normalizeGitURL(urls[0]), nil
	}
	return "", fmt.Errorf("no remote URLs found")
}

// detectHead returns the current branch name and commit SHA.
func detectHead() (branch string, commit string, err error) {
	repo, err := git.PlainOpenWithOptions(".", &git.PlainOpenOptions{DetectDotGit: true})
	if err != nil {
		return "", "", fmt.Errorf("open git repo: %w", err)
	}
	head, err := repo.Head()
	if err != nil {
		return "", "", fmt.Errorf("get HEAD: %w", err)
	}
	return head.Name().Short(), head.Hash().String(), nil
}

// normalizeGitURL converts SSH-style git URLs to HTTPS for BuildBuddy matching.
func normalizeGitURL(url string) string {
	if len(url) > 4 && url[:4] == "git@" {
		rest := url[4:]
		for i, c := range rest {
			if c == ':' {
				url = "https://" + rest[:i] + "/" + rest[i+1:]
				break
			}
		}
	}
	if len(url) > 4 && url[len(url)-4:] == ".git" {
		url = url[:len(url)-4]
	}
	return url
}
