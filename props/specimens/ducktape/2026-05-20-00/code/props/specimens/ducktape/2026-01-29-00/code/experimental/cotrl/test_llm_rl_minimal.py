#!/usr/bin/env python3
"""TEMPORARY ONE-OFF minimal test of LLM RL setup.

Can delete after: Main experiment validation complete
"""

import asyncio
import os
import sys

import gymnasium as gym
import numpy as np
import openai
import pytest_bazel


async def test_minimal():
    """Test basic functionality with one environment."""
    print("Testing LLM RL setup...")

    # Check API key
    if not os.getenv("OPENAI_API_KEY"):
        print("❌ Error: OPENAI_API_KEY environment variable not set")
        return False
    print("✓ API key found")

    # Test environment creation
    try:
        env = gym.make("FrozenLake-v1")
        obs, _info = env.reset()
        print(f"✓ Environment created - observation: {obs}")
        env.close()
    except Exception as e:
        print(f"❌ Environment error: {e}")
        return False

    # Test OpenAI API
    try:
        client = openai.AsyncOpenAI()
        response = await client.chat.completions.create(
            model="gpt-4o-mini", messages=[{"role": "user", "content": "Say 'test'"}], max_tokens=10, temperature=1.0
        )
        print(f"✓ OpenAI API working - response: {response.choices[0].message.content}")
    except Exception as e:
        print(f"❌ OpenAI API error: {e}")
        return False

    # Test numpy array formatting
    test_state = np.array([0.123, -0.456, 0.789])
    state_str = np.array2string(test_state, precision=4, suppress_small=True)
    print(f"✓ Numpy formatting - state: {state_str}")

    print("\n✅ All tests passed! Ready to run full experiment.")
    return True


if __name__ == "__main__":
    success = asyncio.run(test_minimal())
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    pytest_bazel.main()
