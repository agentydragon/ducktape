"""Self-contained Wordle environment for TRL's environment_factory.

Imported by wordle_train.py and the tests. Uses NLTK word lists; no external
game server needed.
"""

from __future__ import annotations

import logging

import nltk
from nltk import pos_tag
from nltk.corpus import words

# Ensure NLTK data is available.
for resource in ["words", "averaged_perceptron_tagger_eng"]:
    try:
        nltk.data.find(f"corpora/{resource}" if resource == "words" else f"taggers/{resource}")
    except LookupError:
        nltk.download(resource, quiet=True)

logger = logging.getLogger(__name__)

MAX_GUESSES = 6
WORD_LENGTH = 5

# 5-letter nouns from NLTK (same approach as TextArena's Wordle).
_all_words = words.words("en-basic")
WORD_LIST = [w.lower() for w in _all_words if len(w) == WORD_LENGTH and pos_tag([w])[0][1] == "NN"]
_VALID_WORDS = {w.lower() for w in words.words("en") if len(w) == WORD_LENGTH and w.isalpha()}
logger.info("Wordle: %d target words, %d valid guesses", len(WORD_LIST), len(_VALID_WORDS))

SYSTEM_PROMPT = """\
You are playing Wordle. Guess the secret 5-letter word in 6 attempts.

After each guess you get feedback per letter:
- G = correct letter, correct position
- Y = correct letter, wrong position
- X = letter not in word

Use the `guess` tool with a lowercase 5-letter English word.\
"""

_game_counter = 0


def _score_guess(secret: str, guess: str) -> list[str]:
    """Return per-letter feedback: G (green), Y (yellow), X (wrong)."""
    result = ["X"] * WORD_LENGTH
    secret_chars = list(secret)
    # First pass: greens
    for i in range(WORD_LENGTH):
        if guess[i] == secret[i]:
            result[i] = "G"
            secret_chars[i] = ""
    # Second pass: yellows
    for i in range(WORD_LENGTH):
        if result[i] == "X" and guess[i] in secret_chars:
            result[i] = "Y"
            secret_chars[secret_chars.index(guess[i])] = ""
    return result


def _completion_score(feedback: list[str]) -> float:
    """Partial reward: greens count full, yellows count half."""
    greens = sum(1 for f in feedback if f == "G")
    yellows = sum(1 for f in feedback if f == "Y")
    return (greens + yellows * 0.5) / WORD_LENGTH


class WordleEnv:
    """Minimal in-process Wordle environment for TRL's environment_factory."""

    def __init__(self):
        global _game_counter  # noqa: PLW0603
        _game_counter += 1
        self._game_id = _game_counter
        self._secret = ""
        self.reward = 0.0
        self.done = False
        self._guess_count = 0
        self._best_score = 0.0
        # Diagnostic counters consumed by the metric_* reward funcs below; reset
        # in reset() so each rollout starts at zero.
        self.n_invalid_length = 0
        self.n_invalid_word = 0
        self.n_already_over = 0
        self._unique_guesses: set[str] = set()
        self.won = False

    def reset(self, seed: int = 0, **_kwargs) -> str | None:
        # seed ensures all G completions within a GRPO group play the same word.
        self._secret = WORD_LIST[seed % len(WORD_LIST)]
        self.reward = 0.0
        self.done = False
        self._guess_count = 0
        self._best_score = 0.0
        self.n_invalid_length = 0
        self.n_invalid_word = 0
        self.n_already_over = 0
        self._unique_guesses = set()
        self.won = False
        logger.info("game %d: secret=%s", self._game_id, self._secret)
        return f"Guess the 5-letter word. You have {MAX_GUESSES} attempts."

    def guess(self, word: str) -> str:
        """Guess a 5-letter word.

        Args:
            word: A lowercase 5-letter English word.

        Returns:
            Feedback for each letter: G (green), Y (yellow), X (wrong).
        """
        if self.done:
            self.n_already_over += 1
            return "Game already over. Stop calling guess."

        word = word.strip().lower().strip("[]")

        if len(word) != WORD_LENGTH or not word.isalpha():
            self.n_invalid_length += 1
            return f"Invalid: must be exactly {WORD_LENGTH} letters."

        if word not in _VALID_WORDS:
            self.n_invalid_word += 1
            return f"'{word}' is not a recognized English word."

        self._guess_count += 1
        self._unique_guesses.add(word)
        remaining = MAX_GUESSES - self._guess_count

        feedback = _score_guess(self._secret, word)
        score = _completion_score(feedback)
        self._best_score = max(self._best_score, score)
        self.reward = self._best_score
        feedback_str = " ".join(feedback)
        won = word == self._secret

        if won:
            self.reward = 1.0
            self.done = True
            self.won = True
            result = f"{word.upper()}: {feedback_str}. Correct!"
        elif remaining == 0:
            self.done = True
            result = f"{word.upper()}: {feedback_str}. Game over, the word was {self._secret}."
        else:
            result = f"{word.upper()}: {feedback_str}. {remaining} guesses left."

        logger.info("game %d [%d/%d] r=%.2f: %s", self._game_id, self._guess_count, MAX_GUESSES, self.reward, result)
        return result


def reward_func(environments, **_kwargs) -> list[float]:
    """TRL reward function adapter: pulls each env's best-so-far reward."""
    return [env.reward for env in environments]


# Metric "reward" functions: TRL logs each reward func's mean/std as a tensorboard
# scalar `train/rewards/<fn_name>/...`. Setting reward_weights=[1, 0, 0, ...] in the
# trainer config keeps these out of the advantage computation; they're free
# diagnostic channels.


def metric_invalid_length(environments, **_kwargs) -> list[float]:
    """Mean count per rollout of wrong-length / non-alpha guesses ('Invalid: ...')."""
    return [float(env.n_invalid_length) for env in environments]


def metric_invalid_word(environments, **_kwargs) -> list[float]:
    """Mean count per rollout of unknown 5-letter tokens ('X is not recognized')."""
    return [float(env.n_invalid_word) for env in environments]


def metric_post_game_over(environments, **_kwargs) -> list[float]:
    """Mean count per rollout of guesses sent after the game ended."""
    return [float(env.n_already_over) for env in environments]


def metric_unique_guesses(environments, **_kwargs) -> list[float]:
    """Per-rollout count of distinct valid words guessed (max MAX_GUESSES)."""
    return [float(len(env._unique_guesses)) for env in environments]


def metric_win(environments, **_kwargs) -> list[float]:
    """1.0 if the rollout solved the puzzle, else 0.0. Mean = win rate."""
    return [float(env.won) for env in environments]


METRIC_FUNCS = [metric_invalid_length, metric_invalid_word, metric_post_game_over, metric_unique_guesses, metric_win]
