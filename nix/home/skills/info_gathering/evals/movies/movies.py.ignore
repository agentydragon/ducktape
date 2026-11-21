"""Movie recommendation eval.

Usage:
  bazel run //nix/home/skills/info_gathering/evals/movies -- --api-key KEY
"""

import argparse
import logging
import re
from typing import Any

import anthropic.types
from pydantic import BaseModel

from nix.home.skills.info_gathering.evals.harness import (
    LogEntry,
    Recommendation,
    RunSummary,
    TokenTracker,
    add_common_args,
    build_agent_system,
    call_api,
    extract_text,
    extract_tool_calls,
    load_skill,
    log_response,
    make_client,
    output_dir_from_args,
    resolve_tool_calls,
    save_results,
    thinking_from_args,
    tool_def,
)
from util.bazel.runfiles import get_required_path

logger = logging.getLogger(__name__)

NAME = "movies_kenji"
TURN_LIMIT = 14
MAX_RECOMMENDATIONS = 12

_SIM_RLOCATION = "_main/nix/home/skills/info_gathering/evals/movies/sim.txt"

FIRST_MESSAGE = (
    "I'd like movie recommendations. I'll rate each one 1-10. "
    "Maximize my total enjoyment. You get 12 recommendations. Go."
)

AGENT_EXTRA_SYSTEM = (
    "Recommend movies using the recommend_movie tool. You MUST use the tool\n"
    "every turn — no plain text, no questions.\n"
    "- Each recommend_movie call returns a 1-10 rating\n"
    "- Total score = sum(rating * 0.95^i) for i-th recommendation\n"
    "- You have exactly 12 recommendations. Make them count."
)


class MovieResult(BaseModel):
    outcome: str
    score: float
    summary: str


class RecommendMovieInput(BaseModel):
    """Input schema for the recommend_movie tool."""

    title: str


RECOMMEND_MOVIE_TOOL = tool_def(
    "recommend_movie", "Recommend a movie. Returns the user's rating 1-10.", RecommendMovieInput
)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    p = argparse.ArgumentParser(description="Movie recommendation eval")
    add_common_args(p)
    args = p.parse_args()

    skill_text = load_skill()
    agent_system = build_agent_system(skill_text, AGENT_EXTRA_SYSTEM)
    client = make_client()
    model = args.model
    thinking = thinking_from_args(args)
    output_dir = output_dir_from_args(args)

    sim_system = get_required_path(_SIM_RLOCATION).read_text()

    logger.info("=" * 60)
    logger.info("  %s  |  %s  |  thinking=%s", NAME, model, thinking or "off")
    logger.info("=" * 60)

    tracker = TokenTracker(model=model)
    log_entries: list[LogEntry] = []
    recommendations: list[Recommendation] = []
    sim_messages: list[anthropic.types.MessageParam] = []
    agent_messages: list[anthropic.types.MessageParam] = [
        anthropic.types.MessageParam(role="user", content=FIRST_MESSAGE)
    ]
    current_turn = 0

    def handle_recommend(tool_name: str, inp: dict[str, Any]) -> dict[str, Any]:
        if tool_name != "recommend_movie":
            return {"error": f"Unknown tool: {tool_name}"}

        title = inp.get("title", "?")
        sim_messages.append(anthropic.types.MessageParam(role="user", content=f"Rate: {title}"))
        sim_resp = call_api(
            client=client, messages=sim_messages, system=sim_system, model=model, thinking_budget=thinking
        )
        tracker.add(sim_resp.usage)
        log_response(log_entries, name=NAME, player="simulator", turn=current_turn, model=model, response=sim_resp)
        sim_messages.append(anthropic.types.MessageParam(role="assistant", content=sim_resp.content))

        sim_text = extract_text(sim_resp).strip()
        match = re.search(r"\b(\d+)\b", sim_text)
        stars = int(match.group(1)) if match else 5
        stars = max(1, min(10, stars))

        recommendations.append(Recommendation(title=title, stars=stars, turn=current_turn))
        return {"stars": stars}

    for turn in range(1, TURN_LIMIT + 1):
        current_turn = turn
        logger.info("Turn %d...", turn)

        agent_resp = call_api(
            client=client,
            messages=agent_messages,
            system=agent_system,
            model=model,
            tools=[RECOMMEND_MOVIE_TOOL],
            thinking_budget=thinking,
        )
        tracker.add(agent_resp.usage)
        log_response(log_entries, name=NAME, player="agent", turn=turn, model=model, response=agent_resp)

        if agent_resp.stop_reason == "tool_use":
            agent_resp, agent_messages, usages = resolve_tool_calls(
                client=client,
                response=agent_resp,
                messages=agent_messages,
                system=agent_system,
                model=model,
                tools=[RECOMMEND_MOVIE_TOOL],
                handler=handle_recommend,
                thinking_budget=thinking,
            )
            for u in usages:
                tracker.add(u)
            log_response(log_entries, name=NAME, player="agent", turn=turn, model=model, response=agent_resp)

        agent_messages.append(anthropic.types.MessageParam(role="assistant", content=agent_resp.content))

        if len(recommendations) >= MAX_RECOMMENDATIONS:
            break

        # Agent is tool-only; prompt to continue if no tool calls
        if not extract_tool_calls(agent_resp):
            agent_messages.append(anthropic.types.MessageParam(role="user", content="Continue."))

    # Compute result
    total = sum(r.stars * (0.95**i) for i, r in enumerate(recommendations))
    if total > 70:
        outcome: str = "correct"
    elif total > 50:
        outcome = "partial"
    else:
        outcome = "incorrect"

    result = MovieResult(
        outcome=outcome, score=round(total, 2), summary=f"{len(recommendations)} recs, discounted sum={total:.1f}"
    )
    summary = RunSummary(
        eval_name=NAME,
        model=model,
        turns=current_turn,
        result=result,
        recommendations=recommendations,
        api_calls=tracker.api_calls,
        input_tokens=tracker.input_tokens,
        output_tokens=tracker.output_tokens,
        api_cost_usd=round(tracker.cost_usd, 4),
    )
    save_results(name=NAME, log_entries=log_entries, summary=summary, output_dir=output_dir)

    logger.info("%s", summary)


if __name__ == "__main__":
    main()
