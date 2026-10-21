package policies

import (
	"context"
	"fmt"
	"log/slog"
	"regexp"
	"strings"

	"golang.org/x/text/cases"

	"github.com/agentydragon/ducktape/agentplane/action_service/store"
)

// The GitHub kinds decide over the one repository a call to the upstream GitHub MCP server targets. Both resist the
// same smuggling: an approved or confirmed-public owner/repo pair beside a search query naming another repository in
// its own `repo:` qualifier, which GitHub Search prefers over the separate arguments.

const (
	codeSearchTool = "search_code"
	pullSearchTool = "search_pull_requests"
	// space is a whitespace character: Unicode's, with the information separators U+001C to U+001F.
	space = `[\t-\r\x1c-\x1f\x85\p{Z}]`
	// nameCharacter is a character of an owner or repository name in a code search qualifier: `[A-Za-z0-9_.-]`
	// matched case-insensitively, so it takes the letters whose case mapping is an ASCII letter. `(?i)` would leave out
	// U+0130 and U+0131, which case-map to ASCII letters but do not case-fold to them.
	nameCharacter = `[A-Za-z0-9_.\-\x{130}\x{131}\x{17F}\x{212A}]`
)

var (
	// searchRepositoryQualifier finds each `repo:` GitHub Search may read as a repository qualifier: at the start or
	// after a character that is not a word character. search_pull_requests adds `repo:<owner>/<repo>` from its
	// owner/repo arguments only when the query holds no qualifier of its own.
	searchRepositoryQualifier = regexp.MustCompile(`(?i)(?:^|[^\p{L}\p{N}_])repo:`)
	// codeSearchRepositoryQualifier is the one qualifier search_code may carry, which has no owner/repo arguments: an
	// unquoted, whitespace-delimited `repo:owner/repo`. It is deliberately narrow, since a syntax GitHub may read as
	// free text cannot establish standing authority.
	codeSearchRepositoryQualifier = regexp.MustCompile(
		`(?:^|` + space + `)(?i:repo):(` + nameCharacter + `+/` + nameCharacter + `+)(?:$|` + space + `)`,
	)
)

// GitHubRepository matches a listed GitHub MCP Action whose one target repository is Owner/Repository.
type GitHubRepository struct {
	Owner      string
	Repository string
}

// GitHubPublicRepository matches a listed GitHub MCP Action whose one target repository a live unauthenticated GitHub
// lookup confirms public. A lookup that fails is no match, never a deny.
type GitHubPublicRepository struct{}

func (GitHubRepository) Type() store.PolicyKind       { return store.PolicyGitHubRepository }
func (GitHubPublicRepository) Type() store.PolicyKind { return store.PolicyGitHubPublicRepository }

func (k GitHubRepository) evaluate(_ context.Context, action store.ActionIdentity, arguments map[string]any, _ *Visibility) Outcome {
	target, mismatch := targetRepository(action.Name, arguments)
	if mismatch != nil {
		return *mismatch
	}
	if fold(target.Owner) != fold(k.Owner) || fold(target.Repository) != fold(k.Repository) {
		return NotMatched{Reason: fmt.Sprintf(
			"repository %s/%s is outside %s/%s", target.Owner, target.Repository, k.Owner, k.Repository,
		)}
	}
	return Matched{
		Explanation: fmt.Sprintf("reviewed %s targets repository %s/%s", readKind(action.Name), k.Owner, k.Repository),
		Repository:  &target,
	}
}

func (GitHubPublicRepository) evaluate(ctx context.Context, action store.ActionIdentity, arguments map[string]any, visibility *Visibility) Outcome {
	target, mismatch := targetRepository(action.Name, arguments)
	if mismatch != nil {
		return *mismatch
	}
	public, err := visibility.IsPublic(ctx, target.Owner, target.Repository)
	if err != nil {
		slog.Warn("GitHub repository visibility check unavailable",
			"owner", target.Owner, "repository", target.Repository, "error", err)
		return NotMatched{Reason: fmt.Sprintf("could not confirm %s/%s is public", target.Owner, target.Repository)}
	}
	if !public {
		return NotMatched{Reason: fmt.Sprintf(
			"repository %s/%s is not confirmed public", target.Owner, target.Repository,
		)}
	}
	target.ConfirmedPublic = true
	return Matched{
		Explanation: fmt.Sprintf(
			"reviewed %s targets confirmed-public repository %s/%s", readKind(action.Name), target.Owner, target.Repository,
		),
		Repository: &target,
	}
}

// targetRepository is the one repository a call unambiguously targets, as the call spells it.
func targetRepository(tool string, arguments map[string]any) (store.MatchedRepository, *NotMatched) {
	if tool == codeSearchTool {
		return codeSearchTargetRepository(arguments)
	}
	owner, ownerIsString := arguments["owner"].(string)
	repository, repositoryIsString := arguments["repo"].(string)
	if !ownerIsString || !repositoryIsString {
		return store.MatchedRepository{}, &NotMatched{
			Reason: "call does not identify a repository with string owner/repo arguments",
		}
	}
	if tool == pullSearchTool {
		query, ok := arguments["query"].(string)
		if !ok {
			return store.MatchedRepository{}, &NotMatched{Reason: "pull-request search requires a string query"}
		}
		if searchRepositoryQualifier.MatchString(query) {
			return store.MatchedRepository{}, &NotMatched{
				Reason: "pull-request search query sets a repository qualifier; omit it so owner/repo scopes the search",
			}
		}
	}
	return store.MatchedRepository{Owner: owner, Repository: repository}, nil
}

// codeSearchTargetRepository requires every syntactic `repo:` of the query to be the one narrow qualifier, which
// refuses quoted, negated, repeated and malformed qualifiers rather than guessing how GitHub Search reads them.
func codeSearchTargetRepository(arguments map[string]any) (store.MatchedRepository, *NotMatched) {
	query, ok := arguments["query"].(string)
	if !ok {
		return store.MatchedRepository{}, &NotMatched{Reason: "code search requires a string query"}
	}
	if len(searchRepositoryQualifier.FindAllStringIndex(query, -1)) != 1 {
		return store.MatchedRepository{}, &NotMatched{Reason: "code search requires exactly one repository qualifier"}
	}
	// With one qualifier there is at most one match, so the delimiters this pattern consumes hide none.
	match := codeSearchRepositoryQualifier.FindStringSubmatch(query)
	if match == nil {
		return store.MatchedRepository{}, &NotMatched{
			Reason: "code search requires one unquoted repo:owner/repo qualifier",
		}
	}
	owner, repository, _ := strings.Cut(match[1], "/")
	return store.MatchedRepository{Owner: owner, Repository: repository}, nil
}

func readKind(tool string) string {
	if tool == codeSearchTool {
		return "code search"
	}
	return "read"
}

// fold is a GitHub owner or repository name as GitHub compares it: case-insensitively.
func fold(name string) string {
	return cases.Fold().String(name)
}
