#!/usr/bin/env python3
"""
Advanced parallel demo using actual concurrent processes.

This demonstrates:
- True parallel execution with multiprocessing
- Real-time progress monitoring
- Complex agent interactions
- Critique loops between agents
"""

import multiprocessing
import random
import subprocess
import sys
import time


def analyzer_agent(team_id: str, agent_num: int, work_items: list, result_queue):
    """Analyzer agent that processes work items."""
    agent_name = f"analyzer{agent_num}"

    # Initial status
    subprocess.run(
        [
            "ai-teams",
            "send",
            team_id,
            agent_name,
            "STATUS",
            f"Starting analysis of {len(work_items)} components",
        ],
        check=False,
    )

    findings = []

    for i, item in enumerate(work_items):
        # Simulate work with random duration
        work_time = random.uniform(0.5, 2.0)
        time.sleep(work_time)

        # Random findings
        if random.random() < 0.3:
            finding = f"Issue in {item}: {random.choice(['Memory leak', 'Race condition', 'Null pointer', 'Buffer overflow'])}"
            findings.append(finding)

            subprocess.run(
                ["ai-teams", "send", team_id, agent_name, "DISCOVERY", finding],
                check=False,
            )

        # Progress update every 2 items
        if (i + 1) % 2 == 0:
            subprocess.run(
                [
                    "ai-teams",
                    "send",
                    team_id,
                    agent_name,
                    "PROGRESS",
                    f"Analyzed {i + 1}/{len(work_items)} components",
                ],
                check=False,
            )

        # Random collaboration
        if random.random() < 0.15:
            subprocess.run(
                [
                    "ai-teams",
                    "send",
                    team_id,
                    agent_name,
                    "FYI",
                    f"Seeing pattern similar to analyzer{(agent_num % 3) + 1}'s findings",
                ],
                check=False,
            )

    # Complete
    subprocess.run(
        [
            "ai-teams",
            "send",
            team_id,
            agent_name,
            "COMPLETE",
            f"Found {len(findings)} issues in {len(work_items)} components",
        ],
        check=False,
    )

    result_queue.put((agent_name, findings))


def reviewer_agent(team_id: str, analyzer_count: int, result_queue):
    """Reviewer agent that critiques analyzer findings."""
    agent_name = "reviewer"

    time.sleep(2)  # Let analyzers start

    subprocess.run(
        [
            "ai-teams",
            "send",
            team_id,
            agent_name,
            "STATUS",
            f"Monitoring findings from {analyzer_count} analyzers",
        ],
        check=False,
    )

    # Periodically review and critique
    critiques_sent = 0
    for _ in range(5):  # 5 review cycles
        time.sleep(3)

        critique_target = f"analyzer{random.randint(1, analyzer_count)}"
        critique = random.choice(
            [
                "Consider checking for side effects",
                "This might be a false positive - verify manual test",
                "Good catch! Mark as high priority",
                "Similar issue fixed in commit abc123",
            ],
        )

        subprocess.run(
            [
                "ai-teams",
                "send",
                team_id,
                agent_name,
                "CRITIQUE",
                f"@{critique_target}: {critique}",
            ],
            check=False,
        )
        critiques_sent += 1

    subprocess.run(
        [
            "ai-teams",
            "send",
            team_id,
            agent_name,
            "COMPLETE",
            f"Review complete, sent {critiques_sent} critiques",
        ],
        check=False,
    )

    result_queue.put((agent_name, f"{critiques_sent} critiques"))


def coordinator_agent(team_id: str, total_components: int):
    """Coordinator agent that monitors overall progress."""
    agent_name = "coordinator"

    subprocess.run(
        [
            "ai-teams",
            "send",
            team_id,
            agent_name,
            "STATUS",
            f"Coordinating analysis of {total_components} components",
        ],
        check=False,
    )

    # Monitor for 20 seconds
    start_time = time.time()
    status_count = 0

    while time.time() - start_time < 20:
        time.sleep(5)
        status_count += 1

        subprocess.run(
            [
                "ai-teams",
                "send",
                team_id,
                agent_name,
                "STATUS",
                f"Progress check #{status_count} - monitoring team activity",
            ],
            check=False,
        )

        # Random coordination messages
        if random.random() < 0.3:
            subprocess.run(
                [
                    "ai-teams",
                    "send",
                    team_id,
                    agent_name,
                    "FYI",
                    random.choice(
                        [
                            "CPU usage normal across all agents",
                            "Good progress on critical path items",
                            "Consider load balancing if delays occur",
                        ],
                    ),
                ],
                check=False,
            )

    subprocess.run(
        ["ai-teams", "send", team_id, agent_name, "COMPLETE", "Coordination complete"],
        check=False,
    )


def run_parallel_demo():
    """Run truly parallel agent demonstration."""

    print("🚀 Starting TRUE PARALLEL agent demonstration")
    print("=" * 70)

    # Create team
    result = subprocess.run(
        [
            "ai-teams",
            "create-team",
            "--task",
            "Parallel component analysis with real concurrency",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    team_id = result.stdout.strip()

    print(f"✅ Created team: {team_id}")
    print("=" * 70)

    # Prepare work items
    components = [f"module_{i}" for i in range(30)]

    # Divide work
    chunk_size = len(components) // 3
    work_assignments = [
        components[:chunk_size],
        components[chunk_size : 2 * chunk_size],
        components[2 * chunk_size :],
    ]

    print(f"\n📊 Analyzing {len(components)} components with:")
    print("   - 3 analyzer agents (parallel)")
    print("   - 1 reviewer agent (critiques findings)")
    print("   - 1 coordinator agent (monitors progress)")
    print("\n🔄 Starting all agents concurrently...\n")

    # Create queues and processes
    result_queue = multiprocessing.Queue()
    processes = []

    # Start analyzer agents
    for i, work in enumerate(work_assignments, 1):
        p = multiprocessing.Process(
            target=analyzer_agent,
            args=(team_id, i, work, result_queue),
        )
        p.start()
        processes.append(p)
        print(f"   ✓ Started analyzer{i}")

    # Start reviewer agent
    p = multiprocessing.Process(target=reviewer_agent, args=(team_id, 3, result_queue))
    p.start()
    processes.append(p)
    print("   ✓ Started reviewer")

    # Start coordinator agent
    p = multiprocessing.Process(
        target=coordinator_agent,
        args=(team_id, len(components)),
    )
    p.start()
    processes.append(p)
    print("   ✓ Started coordinator")

    print("\n⏳ Agents working in parallel (this will take ~20 seconds)...")
    print("   Watch the real-time communication with:")
    print(f"   ai-teams channel {team_id} --last 10")
    print()

    # Wait for all processes
    for p in processes:
        p.join()

    # Collect results
    results = []
    while not result_queue.empty():
        results.append(result_queue.get())

    print("\n" + "=" * 70)
    print("✅ All agents completed!")
    print("\n📊 Results summary:")
    for agent, outcome in results:
        if isinstance(outcome, list):
            print(f"   {agent}: Found {len(outcome)} issues")
        else:
            print(f"   {agent}: {outcome}")

    print("\n📋 View complete communication log:")
    print(f"   ai-teams channel {team_id}")
    print("\n💡 Try running 'ai-teams list' to see all teams")
    print("=" * 70)


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--help":
        print(__doc__)
        return

    run_parallel_demo()


if __name__ == "__main__":
    main()
