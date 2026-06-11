"""Tests for the Wordle environment and scoring logic."""

from __future__ import annotations

from wordle_env import MAX_GUESSES, WORD_LENGTH, WORD_LIST, WordleEnv, _completion_score, _score_guess


class TestScoreGuess:
    def test_exact_match(self):
        assert _score_guess("crane", "crane") == ["G", "G", "G", "G", "G"]

    def test_all_wrong(self):
        assert _score_guess("crane", "lymph") == ["X", "X", "X", "X", "X"]

    def test_yellow(self):
        # 'a' is in 'crane' but not at position 0
        result = _score_guess("crane", "about")
        assert result[0] == "Y"  # a is in crane at position 2

    def test_green_and_yellow(self):
        result = _score_guess("crane", "candy")
        assert result[0] == "G"  # c correct
        assert result[1] == "Y"  # a in word but wrong position
        assert result[2] == "Y"  # n in word but wrong position

    def test_duplicate_letter_one_green(self):
        # Secret has one 'e', guess has two 'e's — only the green one should match.
        result = _score_guess("crane", "eagle")
        # e at position 0: not in position 0 of secret, but 'e' is at position 4
        # e at position 4: matches position 4 -> G
        assert result[4] == "G"
        # first 'e' should be X since the only 'e' is consumed by the green
        assert result[0] == "X"

    def test_duplicate_letter_in_secret(self):
        # Secret "hello" has two l's. Guess "llama" — both l's match (yellow).
        result = _score_guess("hello", "llama")
        assert result[0] == "Y"  # l exists but not at position 0
        assert result[1] == "Y"  # second l also in word (hello has l at pos 2 and 3)

    def test_duplicate_letter_in_guess_single_in_secret(self):
        # Secret "crane" has one 'a' at position 2. Guess "llama" has 'a' at positions 2 and 4.
        result = _score_guess("crane", "llama")
        assert result[2] == "G"  # a at position 2 matches exactly
        assert result[4] == "X"  # second a: the only 'a' was consumed by the green


class TestCompletionScore:
    def test_all_green(self):
        assert _completion_score(["G", "G", "G", "G", "G"]) == 1.0

    def test_all_wrong(self):
        assert _completion_score(["X", "X", "X", "X", "X"]) == 0.0

    def test_mixed(self):
        # 2 greens + 1 yellow = (2 + 0.5) / 5 = 0.5
        assert _completion_score(["G", "G", "Y", "X", "X"]) == 0.5

    def test_all_yellow(self):
        # 5 yellows = (0 + 5*0.5) / 5 = 0.5
        assert _completion_score(["Y", "Y", "Y", "Y", "Y"]) == 0.5


class TestWordList:
    def test_all_words_correct_length(self):
        for word in WORD_LIST:
            assert len(word) == WORD_LENGTH, f"{word!r} has length {len(word)}"

    def test_all_words_lowercase_alpha(self):
        for word in WORD_LIST:
            assert word.isalpha(), f"{word!r} is not alpha"
            assert word.islower(), f"{word!r} is not lowercase"

    def test_has_reasonable_size(self):
        assert len(WORD_LIST) >= 50


class TestWordleEnv:
    def make_env(self, seed: int = 0) -> WordleEnv:
        env = WordleEnv()
        env.reset(seed=seed)
        return env

    def test_reset_returns_prompt(self):
        env = WordleEnv()
        obs = env.reset(seed=0)
        assert "Guess" in obs
        assert str(MAX_GUESSES) in obs

    def test_correct_guess_wins(self):
        env = self.make_env(seed=0)
        secret = env._secret
        result = env.guess(secret)
        assert "Correct!" in result
        assert env.reward == 1.0
        assert env.done is True

    def test_six_wrong_guesses_ends_game(self):
        env = self.make_env(seed=0)
        secret = env._secret
        # Pick a word that's definitely not the secret
        wrong = next(w for w in WORD_LIST if w != secret)
        result = ""
        for _ in range(MAX_GUESSES):
            result = env.guess(wrong)
        assert env.done is True
        assert "Game over" in result
        assert env.reward >= 0.0  # partial credit

    def test_partial_reward_after_nonterminal_guess(self):
        env = self.make_env(seed=0)
        secret = env._secret
        wrong = next(w for w in WORD_LIST if w != secret and _completion_score(_score_guess(secret, w)) > 0.0)
        result = env.guess(wrong)
        assert "guesses left" in result
        assert env.done is False
        assert env.reward == _completion_score(_score_guess(secret, wrong))

    def test_invalid_guess_preserves_best_reward(self):
        env = self.make_env(seed=0)
        secret = env._secret
        wrong = next(w for w in WORD_LIST if w != secret and _completion_score(_score_guess(secret, w)) > 0.0)
        env.guess(wrong)
        reward_after_valid_guess = env.reward
        result = env.guess("zzzzz")
        assert "not a recognized" in result
        assert env.reward == reward_after_valid_guess

    def test_post_done_returns_polite_string(self):
        # After the game ends we don't raise — TRL's _tool_call_loop swallows tool
        # exceptions into {"error": str(e)} which is just noise to the model. Returning
        # a plain string keeps the rollout's transcript clean. The iteration cap
        # (max_tool_calling_iterations=MAX_GUESSES) is what actually bounds the rollout.
        env = self.make_env(seed=0)
        secret = env._secret
        env.guess(secret)  # win
        result = env.guess("about")
        assert "already over" in result.lower()

    def test_invalid_length_doesnt_count(self):
        env = self.make_env(seed=0)
        result = env.guess("hi")
        assert "Invalid" in result
        assert env._guess_count == 0  # didn't consume a turn

    def test_invalid_word_doesnt_count(self):
        env = self.make_env(seed=0)
        result = env.guess("zzzzz")
        assert "not a recognized" in result
        assert env._guess_count == 0

    def test_strips_brackets(self):
        env = self.make_env(seed=0)
        secret = env._secret
        result = env.guess(f"[{secret}]")
        assert "Correct!" in result

    def test_same_seed_same_word(self):
        env1 = self.make_env(seed=42)
        env2 = self.make_env(seed=42)
        assert env1._secret == env2._secret

    def test_different_seed_likely_different_word(self):
        env1 = self.make_env(seed=0)
        env2 = self.make_env(seed=1)
        # Not guaranteed but very likely with 150+ words
        assert env1._secret != env2._secret

    def test_partial_reward_on_loss(self):
        env = self.make_env(seed=0)
        secret = env._secret
        # Pick a word that shares some letters with the secret for partial credit
        wrong = next(w for w in WORD_LIST if w != secret and any(c in secret for c in w))
        for _ in range(MAX_GUESSES):
            if not env.done:
                env.guess(wrong)
        assert env.reward > 0.0  # should get partial credit from shared letters


if __name__ == "__main__":
    import pytest_bazel

    pytest_bazel.main()
