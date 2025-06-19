#!/usr/bin/env python3
"""
Test scenario: Analyze and refactor a large codebase

This scenario demonstrates:
- Parallel analysis by multiple agents
- Communication and discoveries
- Handoffs between agents
- Blockers and resolutions
- Progress tracking
"""

import random
import subprocess
import sys
import time
from typing import TypedDict


class FileInfo(TypedDict):
    issues: list[str]
    complexity: int


# Test data - simulated files to analyze
MOCK_CODEBASE: dict[str, FileInfo] = {
    "src/auth/login.py": {
        "issues": ["SQL injection vulnerability", "Hardcoded credentials"],
        "complexity": 8,
    },
    "src/auth/session.py": {
        "issues": ["Weak session tokens", "No expiration"],
        "complexity": 6,
    },
    "src/api/users.py": {
        "issues": ["No rate limiting", "Missing input validation"],
        "complexity": 7,
    },
    "src/api/products.py": {
        "issues": ["N+1 query problem", "No caching"],
        "complexity": 5,
    },
    "src/utils/crypto.py": {
        "issues": ["MD5 hash usage", "Weak key generation"],
        "complexity": 9,
    },
    "src/utils/email.py": {
        "issues": ["No SPF validation", "Template injection"],
        "complexity": 4,
    },
    "src/db/models.py": {
        "issues": ["Missing indexes", "No constraints"],
        "complexity": 6,
    },
    "src/db/migrations.py": {
        "issues": ["No rollback support", "Data loss risk"],
        "complexity": 7,
    },
}


def simulate_agent_work(team_id: str, agent_name: str, assigned_files: list):
    """Simulate an agent analyzing files and reporting findings."""

    print(f"\n🤖 {agent_name} starting work on {len(assigned_files)} files...")

    # Initial status
    subprocess.run(
        [
            "ai-teams",
            "send",
            team_id,
            agent_name,
            "STATUS",
            f"Starting analysis of {len(assigned_files)} files",
        ],
        check=False,
    )

    total_issues = 0
    critical_issues = []

    for i, filepath in enumerate(assigned_files):
        file_info = MOCK_CODEBASE[filepath]

        # Simulate analysis time based on complexity
        analysis_time = file_info["complexity"] * 0.1
        time.sleep(analysis_time)

        # Report progress
        progress = (i + 1) / len(assigned_files) * 100
        subprocess.run(
            [
                "ai-teams",
                "send",
                team_id,
                agent_name,
                "PROGRESS",
                f"Analyzed {filepath} ({progress:.0f}% complete)",
            ],
            check=False,
        )

        # Check for critical issues
        for issue in file_info["issues"]:
            total_issues += 1
            if "injection" in issue or "credentials" in issue or "crypto" in issue:
                critical_issues.append((filepath, issue))

        # Simulate random events
        if random.random() < 0.2:  # 20% chance of discovery
            subprocess.run(
                [
                    "ai-teams",
                    "send",
                    team_id,
                    agent_name,
                    "DISCOVERY",
                    f"Pattern detected: {random.choice(['Repeated error handling', 'Common anti-pattern', 'Performance bottleneck'])}",
                ],
                check=False,
            )

        if random.random() < 0.1:  # 10% chance of blocker
            subprocess.run(
                [
                    "ai-teams",
                    "send",
                    team_id,
                    agent_name,
                    "BLOCKER",
                    f"Cannot analyze {filepath}: {random.choice(['Missing dependencies', 'Syntax errors', 'Access denied'])}",
                ],
                check=False,
            )
            time.sleep(1)
            # Simulate resolution
            subprocess.run(
                [
                    "ai-teams",
                    "send",
                    team_id,
                    agent_name,
                    "BLOCKER_RESOLVED",
                    "Issue resolved, continuing analysis",
                ],
                check=False,
            )

    # Report findings
    if critical_issues:
        subprocess.run(
            [
                "ai-teams",
                "send",
                team_id,
                agent_name,
                "DISCOVERY",
                f"Found {len(critical_issues)} CRITICAL security issues requiring immediate attention",
            ],
            check=False,
        )

        # Request handoff to security specialist
        subprocess.run(
            [
                "ai-teams",
                "send",
                team_id,
                agent_name,
                "HANDOFF",
                f"Need security specialist to review {len(critical_issues)} critical findings",
            ],
            check=False,
        )

    # Final status
    subprocess.run(
        [
            "ai-teams",
            "send",
            team_id,
            agent_name,
            "COMPLETE",
            f"Analysis complete: {total_issues} issues found in {len(assigned_files)} files",
        ],
        check=False,
    )

    return total_issues, critical_issues


def simulate_security_specialist(team_id: str, handoff_from: str):
    """Simulate security specialist responding to handoff."""

    time.sleep(2)  # Simulate delay before specialist notices

    subprocess.run(
        [
            "ai-teams",
            "send",
            team_id,
            "security",
            "HANDOFF_ACCEPTED",
            f"Taking over security review from {handoff_from}",
        ],
        check=False,
    )

    subprocess.run(
        [
            "ai-teams",
            "send",
            team_id,
            "security",
            "STATUS",
            "Reviewing critical security findings",
        ],
        check=False,
    )

    time.sleep(3)  # Simulate review time

    subprocess.run(
        [
            "ai-teams",
            "send",
            team_id,
            "security",
            "DISCOVERY",
            "Confirmed: 3 HIGH severity vulnerabilities requiring patches",
        ],
        check=False,
    )

    subprocess.run(
        [
            "ai-teams",
            "send",
            team_id,
            "security",
            "COMPLETE",
            "Security review complete, patches documented in SEC-2024-001",
        ],
        check=False,
    )


def simulate_architect_review(team_id: str):
    """Simulate architect doing final review."""

    time.sleep(1)

    subprocess.run(
        [
            "ai-teams",
            "send",
            team_id,
            "architect",
            "STATUS",
            "Reviewing all findings for architectural improvements",
        ],
        check=False,
    )

    time.sleep(2)

    subprocess.run(
        [
            "ai-teams",
            "send",
            team_id,
            "architect",
            "DISCOVERY",
            "Identified 4 areas for architectural refactoring",
        ],
        check=False,
    )

    subprocess.run(
        [
            "ai-teams",
            "send",
            team_id,
            "architect",
            "FYI",
            "Created refactoring plan in ARCH-REFACTOR-2024.md",
        ],
        check=False,
    )

    subprocess.run(
        [
            "ai-teams",
            "send",
            team_id,
            "architect",
            "COMPLETE",
            "Architectural review complete",
        ],
        check=False,
    )


def run_parallel_analysis():
    """Run the complete parallel analysis scenario."""

    print("🚀 Starting parallel codebase analysis scenario...")
    print("=" * 60)

    # Create team
    result = subprocess.run(
        ["ai-teams", "create-team", "--task", "Analyze and refactor legacy codebase"],
        capture_output=True,
        text=True,
        check=False,
    )
    team_id = result.stdout.strip()

    print(f"✅ Created team: {team_id}")
    print(f"📊 Analyzing {len(MOCK_CODEBASE)} files across multiple agents")
    print("=" * 60)

    # Divide work among agents
    all_files = list(MOCK_CODEBASE.keys())

    # Agent assignments
    assignments = {
        "analyzer1": all_files[:3],  # First 3 files
        "analyzer2": all_files[3:6],  # Next 3 files
        "analyzer3": all_files[6:],  # Remaining files
    }

    # Simulate parallel work
    print("\n🔄 Starting parallel analysis...")

    # In real scenario, these would be actual parallel processes
    # For demo, we'll simulate with sequential calls

    for agent, files in assignments.items():
        # In practice, you'd spawn these as separate processes
        total, critical = simulate_agent_work(team_id, agent, files)

        # If critical issues found, trigger security specialist
        if critical and agent == "analyzer1":  # Only one handoff for demo
            simulate_security_specialist(team_id, agent)

    # Architect does final review
    simulate_architect_review(team_id)

    print("\n" + "=" * 60)
    print("📋 Analysis complete! View the communication channel:")
    print(f"   ai-teams channel {team_id}")
    print("\nOr see the last 20 messages:")
    print(f"   ai-teams channel {team_id} --last 20")
    print("=" * 60)


def main():
    """Run the test scenario."""
    if len(sys.argv) > 1 and sys.argv[1] == "--help":
        print(__doc__)
        return

    run_parallel_analysis()


if __name__ == "__main__":
    main()
