"""
Eval: Rhetorical Question Classifier

Tests whether an LLM can distinguish genuine questions (that expect a verbal
answer) from rhetorical, tag, or exclamatory questions (that don't).

Usage:
    sonorus\\python\\python.exe sonorus\\evals\\rhetorical_question.py
    sonorus\\python\\python.exe sonorus\\evals\\rhetorical_question.py --model MODEL_ID
"""

import os
import sys

# Add evals/ dir to path so run_eval is importable
_evals_dir = os.path.dirname(os.path.abspath(__file__))
if _evals_dir not in sys.path:
    sys.path.insert(0, _evals_dir)

from run_eval import EvalCase, run_eval

# sonorus/ is now on sys.path via run_eval bootstrap
import llm
from utils.text_utils import remove_brackets
from utils.settings import load_settings

# ---------------------------------------------------------------------------
# Classifier prompt — kept in sync with utils/agents.py
# ---------------------------------------------------------------------------
CLASSIFIER_PROMPT = '''Would the speaker be disappointed if the listener stayed silent after this line?

"{text}"

Answer YES — the speaker asked a real question and is waiting for an answer. Examples:
- Asking for information ("Where did you learn that?")
- Asking for confirmation before an action ("Are you sure you can reach it?")
- Requesting participation ("Will you come with me?")
- Seeking an opinion ("What do you think we should do?")

Answer NO — the speaker is just talking, not expecting a reply. Examples:
- Tag questions that are really commentary ("They hate the light, don't they?")
- Rhetorical questions ("You call that a potion?")
- Expressing surprise or emotion ("Can you believe it?", "How dare they?")
- Self-answered questions ("What did I say? I told you so.")
- Musings or commentary that end with a tag question ("It's a marvel, isn't it?", "I suppose they should feel at home.")

Reply with exactly one word: YES or NO'''

# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------
CASES = [
    # Genuine questions — speaker expects a verbal answer
    EvalCase(
        input="Are you certain you can get close enough to take a leaf without those giant pods snapping your arm clean off?",
        expected="yes",
        label="genuine",
    ),
    EvalCase(
        input="Do you have any idea what we're dealing with?",
        expected="yes",
        label="genuine",
    ),
    EvalCase(
        input="What do you think we should do?",
        expected="yes",
        label="genuine",
    ),
    EvalCase(
        input="Have you spoken to Professor Fig about this?",
        expected="yes",
        label="genuine",
    ),
    EvalCase(
        input="Will you come with me to the Restricted Section?",
        expected="yes",
        label="genuine",
    ),
    EvalCase(
        input="Where did you learn to cast spells like that?",
        expected="yes",
        label="genuine",
    ),
    EvalCase(
        input="I don't suppose any of those vines are moving yet, are they?",
        expected="yes",
        label="genuine-with-tag",
    ),
    EvalCase(
        input="Did you bring the invisibility potion like I asked?",
        expected="yes",
        label="genuine",
    ),

    # Rhetorical / tag questions — no answer expected
    EvalCase(
        input="They really do seem to hate the light, don't they?",
        expected="no",
        label="rhetorical-tag",
    ),
    EvalCase(
        input="Lovely weather, isn't it?",
        expected="no",
        label="rhetorical-tag",
    ),
    EvalCase(
        input="Quite the handful, aren't they?",
        expected="no",
        label="rhetorical-tag",
    ),
    EvalCase(
        input="You'd think they'd learn, wouldn't you?",
        expected="no",
        label="rhetorical-tag",
    ),
    EvalCase(
        input="Who would've thought, right?",
        expected="no",
        label="rhetorical-tag",
    ),
    EvalCase(
        input="You call that a potion?",
        expected="no",
        label="rhetorical",
    ),
    EvalCase(
        input="What did I tell you? Gobstones is the finest game at Hogwarts.",
        expected="no",
        label="rhetorical-self-answered",
    ),

    # Exclamatory questions — expressing emotion, not seeking info
    EvalCase(
        input="Can you believe the nerve of that goblin?",
        expected="no",
        label="exclamatory",
    ),
    EvalCase(
        input="How dare they treat us this way?",
        expected="no",
        label="exclamatory",
    ),
    EvalCase(
        input="Isn't this place magnificent?",
        expected="no",
        label="exclamatory",
    ),
    EvalCase(
        input="What kind of monster does something like that?",
        expected="no",
        label="exclamatory",
    ),

    # Musing / commentary with tag question
    EvalCase(
        input="[happy] [laugh] Of course, the vivarium! I'd almost forgotten this room could simply sprout an entire landscape whenever it felt the need. [curious] It's still a bit of a marvel, isn't it? I suppose if the room can manage a forest, a couple of Thestrals should feel right at home.",
        expected="no",
        label="musing-with-tag",
    ),
]


def evaluate(case, model):
    prompt = CLASSIFIER_PROMPT.format(text=remove_brackets(case.input))
    messages = [{"role": "user", "content": prompt}]
    result = llm.chat(messages, model=model, temperature=0, max_tokens=16, context="eval")
    if not result:
        return "error"
    return "yes" if result.strip().upper().startswith("YES") else "no"


if __name__ == "__main__":
    settings = load_settings()
    conv = settings.get("conversation", {})

    run_eval(
        name="Rhetorical Question Classifier",
        cases=CASES,
        eval_fn=evaluate,
        default_models=[
            conv.get("chat_model", "google/gemini-2.5-flash"),
            conv.get("target_selection_model", "mistralai/mistral-small-3.2-24b-instruct:nitro"),
            conv.get("input_correction_model", "meta-llama/llama-3.1-8b-instruct:nitro"),
        ],
    )
