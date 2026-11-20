# Ducktape repository parsimony

Keep Ducktape correct, understandable, and easy to change. A future maintainer should be able to find what they need, understand what must keep working, and make a change without updating several copies of the same fact.

Preserve important user flows, security properties, access to stored data, and the ability to deploy, diagnose, and recover the system. Follow [the consumer scope in AGENTS.md](../../AGENTS.md#changes). An internal protocol can change if the result is easier to understand and maintain. Check the actual consumers, including configuration, jobs, and deployed components. Review feature retirement and reductions in supported behavior explicitly.

Follow the [parsimony levels in STYLE.md](../../STYLE.md#parsimony-levels).

## Keep knowledge findable

- Give each durable fact, decision, or procedure one authoritative home. Put it where the people changing the affected system will look. Link to it from relevant entry points with enough context for the reader to act. Derive repeated machine-readable data from its source wherever practical.
- Retire stale plans, resolved investigations, and redundant explanations. Before removing them, carry forward any still-relevant constraint, diagnostic clue, workaround, or pointer to an open issue. Keep the history in Git or the original PR. [Repository documentation conventions](../../README.md#conventions)
- Specify observable outcomes and the constraints callers rely on. For example, specify that a user can open the app, create a widget, and see its actual current statistics. Pin a lease mechanism, signal, response shape, or error code only when another system depends on it or an invariant requires it.

## Keep the codebase small and consistent

- Use as much code as correctness, readability, maintainability, and coverage of important flows require. Remove dead paths, redundant layers, and repeated logic when doing so preserves those qualities. Share rules that need to change together. Keep independent policies local when a common abstraction would tie them together. Judge a simplification by how much easier it makes the next correct change.
- Reuse established repository mechanisms and well-suited libraries for common work. Follow the existing framework for an HTTP service, such as FastAPI or Huma in the appropriate language, and use established libraries for currency, date and time, and RPC.
- Converge on one set of conventions for each language or area. Weigh the recurring savings against the cost of migration and added dependencies. Let a concrete requirement justify an exception, and document it where future maintainers will encounter it. [Code style guide](../../STYLE.md)

## Keep tests that catch meaningful failures

Exercise important user flows, project-owned behavior, boundary contracts, and regressions that could recur. Keep tests of integrations where our use of a library can fail. Trim tests that merely exercise library basics, restate a declaration, or duplicate existing coverage. When removing a test that catches a plausible regression, preserve that coverage at the appropriate boundary. [Testing guidance](../../STYLE.md#testing)

For each assertion, ask what could break while someone was making an otherwise reasonable change. If `foo = 75` is a tunable choice owned by one declaration, `assert object.foo == 75` adds little. If the application could ignore the setting, vary it and check the resulting behavior. If another live artifact must agree with it, express the shared value once where practical or test the agreement. If a published contract requires exactly `75`, test that value at the boundary.

## Send changes for review

Apply the GitHub label `parsimony` to every PR opened under this goal, including each PR in a stack.

Also keep the association in git, because a label lives only on GitHub. Put `#parsimony` in every commit message of the PR, as the trailer line `Goal: #parsimony`, and on its own line in the PR description. Each merge method carries different text into history. A merge commit keeps the PR's commits. A rebase merge keeps their messages. A squash merge copies the commit messages, or the PR title and description, depending on the repository's setting. This repository's squash merges copy the title and the description, not the commit messages. With the tag in both places, `git log --grep='#parsimony'` finds the merged work for every merge method, unless a squash setting copies only the title.

Pick a bounded area for each pass and prioritize changes that make it easier to find information, change code, or trust behavior. Keep going across passes until you can no longer find changes the repository owner is likely to want or accept.

Open PRs directly for clear low-risk wins, including documentation-only changes and pure refactors. Use a PR stack when it helps avoid conflicts or expresses a dependency. [PR workflow](../../AGENTS.md#splitting-work-into-prs)

Group small related changes into medium or large PRs, roughly 100 to 1,000 changed lines, where that stays easy to review. Group by area or by topic. Examples: all dead code, config and dependencies in one area; all stale pointer and claim repairs; all retirements of one kind of note; all test-helper sharing in one package. Keep each change as its own commit and list the changes in the description, so the owner can review commit by commit. Keep a change in its own PR when it carries material risk, needs an owner decision, or is already large. When several small PRs on one area or topic are pending, replace them with one grouped PR and close the originals.

Open a PR for a promising change with material risk, too. Put the risk or requirement in the title and near the top of the description. Use explicit wording such as `[Migration required]` or `[Ordered rollout]`. Describe the affected consumers and the conditions for merging or deploying. For a protocol change that requires a stack, give the rollout order and when each step is safe to deploy.
