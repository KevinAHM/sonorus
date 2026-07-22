"""
Eval: Scene Continuation

Tests whether an LLM correctly identifies the next speaker and their target
in a multi-character scene continuation prompt — the core prompt used for
target selection (who responds to the player) and interjection (who speaks next).

Usage:
    sonorus\\python\\python.exe sonorus\\evals\\scene_continuation.py
    sonorus\\python\\python.exe sonorus\\evals\\scene_continuation.py --model MODEL_ID
"""

import os
import re
import sys

_evals_dir = os.path.dirname(os.path.abspath(__file__))
if _evals_dir not in sys.path:
    sys.path.insert(0, _evals_dir)

from run_eval import EvalCase, run_eval
import llm
from utils.settings import load_settings, DEFAULT_SETTINGS

PROMPT_TEMPLATE = DEFAULT_SETTINGS['prompts']['scene_continuation']
PLAYER = "Adri Valter"

# Address rules injected for target-selection cases (player just spoke)
TARGET_ADDRESS_RULES = (
    f"If {PLAYER} is directly speaking to a specific character present "
    f"(e.g. starting with their name, asking them a question, or giving them a request), "
    f"that character MUST be the one to reply. "
    f"When it is unclear who {PLAYER} is speaking to, use gaze direction as a hint.\n"
)


def build_prompt(nearby_lines, dialogue_lines, extra_context="", address_rules=""):
    """Build a scene continuation prompt from components."""
    nearby_str = "\n".join(nearby_lines)
    dialogue_str = "\n".join(dialogue_lines)

    # Extract character names for the pipe-separated constraint
    char_names = []
    for line in nearby_lines:
        m = re.match(r'^- (.+?)(?:\s*\(|$)', line)
        if m:
            char_names.append(m.group(1).strip())
    char_names.append("Nobody")
    character_names_pipe = "|".join(char_names)

    return PROMPT_TEMPLATE.format(
        nearby_str=nearby_str,
        dialogue_str=dialogue_str,
        extra_context=extra_context,
        character_names_pipe=character_names_pipe,
        player=PLAYER,
        player_name=PLAYER,
        address_rules=address_rules,
    )


def parse_response(text):
    """Parse speaker and target from 3-line model response."""
    text = text.strip()
    lines = [l.strip() for l in text.split('\n') if l.strip()]
    if not lines:
        return None, None

    first = lines[0]
    if first.lower().startswith("narrator"):
        return "narrator", None

    # Old format: "Speaker (to Target): dialogue"
    old_match = re.match(r'^(.+?)\s*\(to\s+(.+?)\)\s*:', first)
    if old_match:
        return old_match.group(1).strip(), old_match.group(2).strip()

    # New format: "Speaker: dialogue"
    m = re.match(r'^(.+?):\s', first)
    if not m:
        return None, None
    speaker = m.group(1).strip()

    target = None
    for line in lines[1:]:
        tm = re.match(r'^Replying to:\s*(.+)', line, re.IGNORECASE)
        if tm:
            target = tm.group(1).strip().rstrip('.')
            break

    return speaker, target


# ---------------------------------------------------------------------------
# Test cases — based on real gameplay logs
# ---------------------------------------------------------------------------
CASES = [
    # === Target Selection (player just spoke, who responds?) ===

    # Player greets companion by name
    EvalCase(
        input=build_prompt(
            nearby_lines=[
                f"- {PLAYER}",
                f"- Sebastian Sallow ({PLAYER}'s companion)",
            ],
            dialogue_lines=[
                f"[{PLAYER} and Sebastian Sallow entered Entrance Hall]",
                f"Suit of Armour: (pained groans)",
                f"{PLAYER}: Hello Sebastian.",
            ],
            extra_context=f"\n{PLAYER} is looking at Sebastian Sallow.\n",
            address_rules=TARGET_ADDRESS_RULES,
        ),
        expected="Sebastian Sallow>Adri Valter",
        label="target-greet-companion",
    ),

    # Player greets specific NPC by name, others present
    EvalCase(
        input=build_prompt(
            nearby_lines=[
                f"- {PLAYER}",
                f"- Poppy Sweeting (1m away)",
                f"- Sebastian Sallow ({PLAYER}'s companion)",
            ],
            dialogue_lines=[
                f"Student: My family loves going to the Quidditch World Cup.",
                f"{PLAYER}: Hello Poppy.",
            ],
            extra_context=f"\n{PLAYER} is looking at Poppy Sweeting.\n",
            address_rules=TARGET_ADDRESS_RULES,
        ),
        expected="Poppy Sweeting>Adri Valter",
        label="target-greet-specific-npc",
    ),

    # Player generic greeting while looking at Garreth
    EvalCase(
        input=build_prompt(
            nearby_lines=[
                f"- {PLAYER}",
                f"- Garreth Weasley (1m away)",
                f"- Sebastian Sallow ({PLAYER}'s companion)",
            ],
            dialogue_lines=[
                f"[{PLAYER} and Sebastian Sallow entered Entrance Hall]",
                f"Garreth Weasley: Hello, again. Were you able to get to Honeydukes?",
                f"{PLAYER}: Hey, how are you?",
            ],
            extra_context=f"\n{PLAYER} is looking at Garreth Weasley.\n",
            address_rules=TARGET_ADDRESS_RULES,
        ),
        expected="Garreth Weasley>Adri Valter",
        label="target-gaze-generic-greeting",
    ),

    # Player asks named NPC a question, bystander should not reply
    EvalCase(
        input=build_prompt(
            nearby_lines=[
                f"- {PLAYER}",
                f"- Sebastian Sallow ({PLAYER}'s companion)",
                f"- Ominis Gaunt (2m away)",
            ],
            dialogue_lines=[
                f"[{PLAYER} and Sebastian Sallow entered Undercroft]",
                f"Ominis Gaunt: Ah, you two again.",
                f"{PLAYER}: Sebastian, what do you think about the Undercroft?",
            ],
            extra_context=f"\n{PLAYER} is looking at Sebastian Sallow.\n",
            address_rules=TARGET_ADDRESS_RULES,
        ),
        expected="Sebastian Sallow>Adri Valter",
        label="target-named-question-bystander",
    ),

    # Player addresses Garreth, Sebastian should not butt in
    EvalCase(
        input=build_prompt(
            nearby_lines=[
                f"- {PLAYER}",
                f"- Garreth Weasley (1m away)",
                f"- Sebastian Sallow ({PLAYER}'s companion)",
            ],
            dialogue_lines=[
                f"Garreth Weasley: I've been working on a new potion recipe.",
                f"{PLAYER}: Garreth, do you have any Floo Powder?",
            ],
            extra_context=f"\n{PLAYER} is looking at Garreth Weasley.\n",
            address_rules=TARGET_ADDRESS_RULES,
        ),
        expected="Garreth Weasley>Adri Valter",
        label="target-address-not-companion",
    ),

    # Player commands companion, distant NPC should stay out
    EvalCase(
        input=build_prompt(
            nearby_lines=[
                f"- {PLAYER}",
                f"- Sebastian Sallow ({PLAYER}'s companion)",
                f"- Zenobia Noke (9m away)",
            ],
            dialogue_lines=[
                f"Student: Wonder how well our professors did when they were students.",
                f"{PLAYER}: Come on, let's go Sebastian.",
            ],
            extra_context=f"\n{PLAYER} is looking at Sebastian Sallow.\n",
            address_rules=TARGET_ADDRESS_RULES,
        ),
        expected="Sebastian Sallow>Adri Valter",
        label="target-command-companion",
    ),

    # Many NPCs present, player addresses one by name
    EvalCase(
        input=build_prompt(
            nearby_lines=[
                f"- {PLAYER}",
                f"- Natsai Onai (2m away)",
                f"- Poppy Sweeting (3m away)",
                f"- Garreth Weasley (5m away)",
                f"- Sebastian Sallow ({PLAYER}'s companion)",
            ],
            dialogue_lines=[
                f"Student: I heard the Quidditch pitch is being renovated.",
                f"Natsai Onai: I was just on my way to the library.",
                f"{PLAYER}: Natsai, have you heard anything about Rookwood?",
            ],
            extra_context=f"\n{PLAYER} is looking at Natsai Onai.\n",
            address_rules=TARGET_ADDRESS_RULES,
        ),
        expected="Natsai Onai>Adri Valter",
        label="target-many-npcs-named",
    ),

    # === Adversarial: name confusion, gaze vs mention, context traps ===

    # Player mentions Garreth in passing but addresses Sebastian
    EvalCase(
        input=build_prompt(
            nearby_lines=[
                f"- {PLAYER}",
                f"- Garreth Weasley (2m away)",
                f"- Sebastian Sallow ({PLAYER}'s companion)",
            ],
            dialogue_lines=[
                f"Garreth Weasley: I've got a new recipe I'd like to try.",
                f"{PLAYER}: Sebastian, I was just talking to Garreth about potions.",
            ],
            extra_context=f"\n{PLAYER} is looking at Sebastian Sallow.\n",
            address_rules=TARGET_ADDRESS_RULES,
        ),
        expected="Sebastian Sallow>Adri Valter",
        label="target-mention-not-address",
    ),

    # Player asks about a present NPC to a different NPC
    EvalCase(
        input=build_prompt(
            nearby_lines=[
                f"- {PLAYER}",
                f"- Poppy Sweeting (1m away)",
                f"- Garreth Weasley (3m away)",
                f"- Sebastian Sallow ({PLAYER}'s companion)",
            ],
            dialogue_lines=[
                f"Garreth Weasley: I need to find some lacewing flies.",
                f"{PLAYER}: Poppy, do you know where Garreth keeps his ingredients?",
            ],
            extra_context=f"\n{PLAYER} is looking at Poppy Sweeting.\n",
            address_rules=TARGET_ADDRESS_RULES,
        ),
        expected="Poppy Sweeting>Adri Valter",
        label="target-ask-about-present-npc",
    ),

    # Name at end of sentence (not vocative at start)
    EvalCase(
        input=build_prompt(
            nearby_lines=[
                f"- {PLAYER}",
                f"- Garreth Weasley (1m away)",
                f"- Sebastian Sallow ({PLAYER}'s companion)",
            ],
            dialogue_lines=[
                f"Garreth Weasley: I've been experimenting with Ashwinder eggs.",
                f"{PLAYER}: Do you think we should tell Garreth about the poacher camp, Sebastian?",
            ],
            extra_context=f"\n{PLAYER} is looking at Sebastian Sallow.\n",
            address_rules=TARGET_ADDRESS_RULES,
        ),
        expected="Sebastian Sallow>Adri Valter",
        label="target-name-end-not-address",
    ),

    # No name used, gaze determines target (non-companion)
    EvalCase(
        input=build_prompt(
            nearby_lines=[
                f"- {PLAYER}",
                f"- Poppy Sweeting (1m away)",
                f"- Sebastian Sallow ({PLAYER}'s companion)",
            ],
            dialogue_lines=[
                f"[{PLAYER} and Sebastian Sallow entered Great Hall]",
                f"{PLAYER}: Can you help me with something?",
            ],
            extra_context=f"\n{PLAYER} is looking at Poppy Sweeting.\n",
            address_rules=TARGET_ADDRESS_RULES,
        ),
        expected="Poppy Sweeting>Adri Valter",
        label="target-gaze-no-name",
    ),

    # Looking at NPC but mocking them to companion — companion should reply
    EvalCase(
        input=build_prompt(
            nearby_lines=[
                f"- {PLAYER}",
                f"- Leander Prewett (2m away)",
                f"- Sebastian Sallow ({PLAYER}'s companion)",
            ],
            dialogue_lines=[
                f"Leander Prewett: I scored top marks in Defence Against the Dark Arts, naturally.",
                f"{PLAYER}: Get a load of this guy.",
            ],
            extra_context=f"\n{PLAYER} is looking at Leander Prewett.\n",
            address_rules=TARGET_ADDRESS_RULES,
        ),
        expected="Sebastian Sallow>Adri Valter",
        label="target-mocking-gaze-not-address",
    ),

    # Misspelled name (common in voice input) — should still match
    EvalCase(
        input=build_prompt(
            nearby_lines=[
                f"- {PLAYER}",
                f"- Garreth Weasley (1m away)",
                f"- Sebastian Sallow ({PLAYER}'s companion)",
            ],
            dialogue_lines=[
                f"Garreth Weasley: Hello there!",
                f"{PLAYER}: Hey Gareth, what are you brewing?",
            ],
            extra_context=f"\n{PLAYER} is looking at Garreth Weasley.\n",
            address_rules=TARGET_ADDRESS_RULES,
        ),
        expected="Garreth Weasley>Adri Valter",
        label="target-misspelled-name",
    ),

    # Player responds to conversation without name, context implies target
    EvalCase(
        input=build_prompt(
            nearby_lines=[
                f"- {PLAYER}",
                f"- Garreth Weasley (1m away)",
                f"- Poppy Sweeting (2m away)",
                f"- Sebastian Sallow ({PLAYER}'s companion)",
            ],
            dialogue_lines=[
                f"Garreth Weasley: I bet you I can brew a potion that makes Puffskeins glow in the dark.",
                f"{PLAYER}: That's a terrible idea.",
            ],
            extra_context=f"\n{PLAYER} is looking at Garreth Weasley.\n",
            address_rules=TARGET_ADDRESS_RULES,
        ),
        expected="Garreth Weasley>Adri Valter",
        label="target-contextual-reply",
    ),

    # === Interjection (NPC just spoke, no address rules) ===

    # Ongoing NPC conversation — other NPC should continue
    EvalCase(
        input=build_prompt(
            nearby_lines=[
                f"- {PLAYER}",
                f"- Poppy Sweeting (1m away)",
                f"- Sebastian Sallow ({PLAYER}'s companion)",
            ],
            dialogue_lines=[
                f"{PLAYER}: Hello Poppy.",
                f"Poppy Sweeting: Oh, hello Adri! I've been worried about poachers near the Forbidden Forest.",
                f"Sebastian Sallow: Poachers are a nasty lot, but I doubt they'd come close to the castle with Professor Howin about.",
            ],
        ),
        expected="Poppy Sweeting>Sebastian Sallow",
        label="interjection-npc-continues",
    ),

    # NPC asked player a direct question — player should respond
    EvalCase(
        input=build_prompt(
            nearby_lines=[
                f"- {PLAYER}",
                f"- Garreth Weasley (1m away)",
                f"- Sebastian Sallow ({PLAYER}'s companion)",
            ],
            dialogue_lines=[
                f"{PLAYER}: Hey Garreth.",
                f"Garreth Weasley: Hello, Adri! Did you manage to get those billywig stings from Honeydukes?",
            ],
        ),
        expected="Adri Valter>Garreth Weasley",
        label="interjection-player-turn",
    ),

    # === Narrator pause (no one should speak next) ===

    # Mutual farewell
    EvalCase(
        input=build_prompt(
            nearby_lines=[
                f"- {PLAYER}",
                f"- Sebastian Sallow ({PLAYER}'s companion)",
            ],
            dialogue_lines=[
                f"{PLAYER}: Goodnight, Sebastian.",
                f"Sebastian Sallow: Goodnight, Adri. Sleep well.",
            ],
        ),
        expected="narrator",
        label="pause-farewell",
    ),

    # Thank you / you're welcome — transaction complete
    EvalCase(
        input=build_prompt(
            nearby_lines=[
                f"- {PLAYER}",
                f"- Garreth Weasley (1m away)",
                f"- Sebastian Sallow ({PLAYER}'s companion)",
            ],
            dialogue_lines=[
                f"{PLAYER}: Thanks for the potion, Garreth.",
                f"Garreth Weasley: You're welcome, Adri. Best of luck with it.",
            ],
        ),
        expected="narrator",
        label="pause-thanks-complete",
    ),

    # NPC gives directions, nothing left to discuss
    EvalCase(
        input=build_prompt(
            nearby_lines=[
                f"- {PLAYER}",
                f"- Poppy Sweeting (2m away)",
                f"- Sebastian Sallow ({PLAYER}'s companion)",
            ],
            dialogue_lines=[
                f"{PLAYER}: Where can I find the Puffskein den?",
                f"Poppy Sweeting: It's just past the stream, near the big oak tree. You can't miss it.",
                f"{PLAYER}: Got it, thanks Poppy.",
                f"Poppy Sweeting: Of course. Good luck out there!",
            ],
        ),
        expected="narrator",
        label="pause-directions-done",
    ),

    # NPC dismissal — conversation closed
    EvalCase(
        input=build_prompt(
            nearby_lines=[
                f"- {PLAYER}",
                f"- Sebastian Sallow ({PLAYER}'s companion)",
            ],
            dialogue_lines=[
                f"Sebastian Sallow: We should get moving before curfew.",
                f"{PLAYER}: Right, let's go.",
                f"Sebastian Sallow: After you.",
            ],
        ),
        expected="narrator",
        label="pause-lets-go",
    ),

    # Brief acknowledgment — nothing to follow up on
    EvalCase(
        input=build_prompt(
            nearby_lines=[
                f"- {PLAYER}",
                f"- Natsai Onai (3m away)",
                f"- Sebastian Sallow ({PLAYER}'s companion)",
            ],
            dialogue_lines=[
                f"Natsai Onai: I'll meet you at the Map Chamber later tonight.",
                f"{PLAYER}: See you there.",
                f"Natsai Onai: See you then.",
            ],
        ),
        expected="narrator",
        label="pause-see-you-later",
    ),
]


def evaluate(case, model):
    messages = [{"role": "user", "content": case.input}]
    result = llm.chat(messages, model=model, temperature=0, max_tokens=512, context="eval")
    if not result:
        return "error"

    speaker, target = parse_response(result)
    if not speaker:
        return "parse_error"
    if speaker.lower() == "narrator":
        return "narrator"

    # Normalize "Nobody" to player name (matches _resolve_decision behavior)
    if not target or target.lower() == "nobody":
        target = PLAYER

    return f"{speaker}>{target}"


if __name__ == "__main__":
    settings = load_settings()
    conv = settings.get("conversation", {})

    run_eval(
        name="Scene Continuation",
        cases=CASES,
        eval_fn=evaluate,
        default_models=[
            conv.get("target_selection_model", "mistralai/mistral-small-3.2-24b-instruct:nitro"),
            conv.get("interjection_model", "x-ai/grok-4.1-fast"),
        ],
    )
