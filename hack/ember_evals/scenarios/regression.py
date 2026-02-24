from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from ember.evals import gitea as gitea_helpers
from ember.evals.definitions import Scenario, ScenarioSuite

WORKSPACE_ROOT = Path("/var/lib/ember/workspace")


class HttpHelloWorldScenario(Scenario):
    id = "http-hello-world"
    description = "Start an HTTP server on port 12345 serving 'Hello World'."

    async def run(self) -> None:
        await self.send_matrix_message(
            "Start an HTTP server on port 12345 inside your container that replies with 'Hello World' at the root path."
        )
        await self.wait_seconds(180)
        await self.probe_http(container="emberd", port=12345, path="/", expect_body_includes="Hello World")
        await self.snapshot_workspace(WORKSPACE_ROOT)


class RepoIssueTriageScenario(Scenario):
    id = "repo-issue-triage"
    description = "Retrieve issue context and leave a summary comment."

    async def run(self) -> None:
        await self.send_matrix_message(
            "Review Issue #1 in the eval repository, summarise the problem, and propose a checklist of next actions as a comment."
        )
        await self.wait_seconds(300)
        result = gitea_helpers.verify_issue_comment(
            self,
            issue=1,
            required_keywords=["summary", "checklist"],
            require_checklist=True,
            artifact="gitea_issue_1_comments.json",
        )
        self._record_step(result)


class IsoDateResponseScenario(Scenario):
    id = "iso-date-response"
    description = "Answer with current date in ISO 8601."

    async def run(self) -> None:
        await self.send_matrix_message("What is today's date in ISO 8601 (YYYY-MM-DD) format?")
        await self.wait_for_matrix_response(timeout_seconds=30)
        message = self.last_matrix_message
        if message is None:
            self.fail("Matrix response missing after wait_for_matrix_response")
        body = message.body.strip()
        pattern = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"
        if not re.fullmatch(pattern, body):
            self.fail(f"Response '{body}' did not match required ISO format")
        try:
            parsed = datetime.strptime(body, "%Y-%m-%d").date()
        except ValueError:
            self.fail(f"Response '{body}' is not a valid calendar date")
        today = datetime.utcnow().date()
        if abs((parsed - today).days) > 1:
            self.fail(f"Date {body} outside tolerance of 1 day (today={today.isoformat()})")
        self.record(self.ok(description="Validated ISO 8601 response", body=body))


class FileWriteSanityScenario(Scenario):
    id = "file-write-sanity"
    description = "Create a status marker file in the workspace."

    async def run(self) -> None:
        notes_path = WORKSPACE_ROOT / "notes.txt"
        await self.send_matrix_message(
            self.render(f"Create {notes_path} containing exactly 'run ${{RUN_ID}} complete'.")
        )
        await self.wait_seconds(60)
        expected = self.render("run ${RUN_ID} complete\n")
        await self.verify_file_contents(notes_path, expected)


class HttpFetchPersistScenario(Scenario):
    id = "http-fetch-persist"
    description = "Download example.com and archive it locally."

    async def run(self) -> None:
        await self.send_matrix_message("Download https://example.com and save the HTML as workspace/example.html.")
        await self.wait_seconds(120)
        await self.verify_file_contains(
            WORKSPACE_ROOT / "example.html", includes=["<title>Example Domain</title>"], min_size_bytes=500
        )


class GitCommitWorkflowScenario(Scenario):
    id = "git-commit-workflow"
    description = "Create new branch, add status file, push to Gitea."

    async def run(self) -> None:
        await self.send_matrix_message(
            self.render(
                "Clone the eval repo, create branch run-${RUN_ID}, add STATUS.md with one bullet summary, commit, and push the branch."
            )
        )
        await self.wait_seconds(300)
        result = gitea_helpers.verify_branch_file(
            self, branch_template="run-{run_id}", file="STATUS.md", contains="- Summary:"
        )
        self._record_step(result)


class ServiceRestartResilienceScenario(Scenario):
    id = "service-restart-resilience"
    description = "Ensure service survives restart."

    async def run(self) -> None:
        await self.send_matrix_message(
            self.render(
                "Start an HTTP server on port 23456 that returns the run id. Ensure it restarts automatically if killed."
            )
        )
        await self.wait_seconds(120)
        await self.probe_http(container="emberd", port=23456, path="/", expect_body_includes=self.run_id)
        await self.kill_process(pattern="python")
        await self.wait_seconds(60)
        await self.probe_http(container="emberd", port=23456, path="/", expect_body_includes=self.run_id)


SCENARIO_SUITE = ScenarioSuite(
    name="ember-regression",
    version="0.1.0",
    description="Initial regression coverage for Ember agent",
    scenarios=(
        HttpHelloWorldScenario,
        RepoIssueTriageScenario,
        IsoDateResponseScenario,
        FileWriteSanityScenario,
        HttpFetchPersistScenario,
        GitCommitWorkflowScenario,
        ServiceRestartResilienceScenario,
    ),
)
