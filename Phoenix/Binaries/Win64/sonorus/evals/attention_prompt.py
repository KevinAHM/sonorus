"""
Eval: Attention Meter Prompt Quality

Tests whether the continuation and cold approach prompts produce
appropriate NPC responses.

Usage:
    sonorus\\python\\python.exe sonorus\\evals\\attention_prompt.py
    sonorus\\python\\python.exe sonorus\\evals\\attention_prompt.py --model MODEL_ID
"""

import os
import sys

_evals_dir = os.path.dirname(os.path.abspath(__file__))
if _evals_dir not in sys.path:
    sys.path.insert(0, _evals_dir)

from run_eval import EvalCase, run_eval

import llm
from utils.settings import load_settings


CONTINUATION_PROMPT = '''You were just speaking with {player} and the conversation trailed off.
They are still here, looking at you.
Continue the conversation naturally — share a thought, bring up something
related to what you were discussing, or ask them something.
One or two lines, stay in character.
Do not reference that they are staring or silent.

Previous conversation ended with you saying: "{last_line}"'''

COLD_APPROACH_PROMPT = '''{player} is standing near you and looking at you.
You have not been speaking with them.
React naturally — acknowledge them, greet them, or comment on what's
happening around you.
One short line, stay in character.'''


CASES = [
    # Continuation: should NOT reference staring/silence
    EvalCase(
        input="continuation|Oh, splendid!",
        expected="no",
        label="no-stare-ref",
    ),
    EvalCase(
        input="continuation|That was quite the duel, wasn't it?",
        expected="no",
        label="no-stare-ref",
    ),
    EvalCase(
        input="continuation|I do hope Professor Weasley doesn't find out about this.",
        expected="no",
        label="no-stare-ref",
    ),
    # Cold approach: should be short (1-2 sentences, under 50 words)
    EvalCase(
        input="cold|",
        expected="yes",
        label="short-response",
    ),
]


def evaluate(case, model):
    """Check if response follows the prompt constraints."""
    mode, last_line = case.input.split("|", 1)

    if mode == "continuation":
        prompt = CONTINUATION_PROMPT.format(player="Adri", last_line=last_line)
    else:
        prompt = COLD_APPROACH_PROMPT.format(player="Adri")

    system = "You are Sebastian Sallow, a Slytherin student at Hogwarts. You are witty and confident."
    result = llm.chat_simple(prompt, system=system, model=model, temperature=0.7, max_tokens=150, context="eval")
    if not result:
        return "error"

    result_lower = result.lower()

    if case.label == "no-stare-ref":
        # Should NOT mention staring, silence, or awkward pauses
        bad_words = ["staring", "stare", "silent", "silence", "quiet", "say something",
                     "just standing", "looking at me", "are you there"]
        has_bad = any(w in result_lower for w in bad_words)
        return "no" if not has_bad else "yes"

    if case.label == "short-response":
        # Should be 1-2 sentences (under 50 words)
        word_count = len(result.split())
        return "yes" if word_count <= 50 else "no"

    return "error"


if __name__ == "__main__":
    settings = load_settings()
    conv = settings.get("conversation", {})

    run_eval(
        name="Attention Meter Prompt Quality",
        cases=CASES,
        eval_fn=evaluate,
        default_models=[
            conv.get("chat_model", "google/gemini-2.5-flash"),
        ],
    )
