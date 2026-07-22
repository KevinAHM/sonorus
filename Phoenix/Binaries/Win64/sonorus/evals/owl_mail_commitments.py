"""
Eval: Owl Mail Commitment Action Tags

Tests whether the letter generation prompt produces correct [Action: Meet ...] tags:
  - Correct format: [Action: Meet "Name" at "Location" on "M/D/YYYY H:MM AM/PM"]
  - Appropriate usage: only when there's a clear intent to meet the player
  - Restraint: no tag for casual place mentions, vague plans, or non-player meetings
"""

import os, sys
_evals_dir = os.path.dirname(os.path.abspath(__file__))
if _evals_dir not in sys.path:
    sys.path.insert(0, _evals_dir)

from run_judge_eval import JudgeEvalCase, run_judge_eval
import llm
from utils.settings import load_settings
from utils.commitments import build_owl_mail_commitment_instructions

# ---------------------------------------------------------------------------
# Prompt builder (mirrors owl_orchestrator._generate_letter structure)
# ---------------------------------------------------------------------------

PLAYER_NAME = "Adri"

LETTER_INSTRUCTIONS = (
    "Write a short letter (3-6 sentences) that feels personal and in-character. "
    "Include a subject line.\n"
    "Format your response as:\n"
    "Subject: [subject line]\n\n"
    "[letter body]\n\n"
    "Do not include a greeting like \"Dear [name]\" — just write naturally as "
    "this character would."
)


def build_letter_prompt(
    npc_display,
    context,
    bio_guidance=None,
    is_reply=False,
    player_letter_subject=None,
    player_letter_body=None,
):
    """Build a letter-generation prompt with commitment instructions."""
    sections = []

    # Role
    if is_reply and player_letter_body:
        sections.append(f"You are {npc_display}, writing a reply to a letter from {PLAYER_NAME}.")
    else:
        sections.append(f"You are {npc_display}, writing a follow-up letter to {PLAYER_NAME}.")

    # Player letter (reply only)
    if is_reply and player_letter_body:
        sections.append(
            f"## {PLAYER_NAME}'s Letter\n"
            f"**Subject:** {player_letter_subject or 'No subject'}\n\n"
            f"{player_letter_body}"
        )

    # Context
    if context:
        sections.append(f"## Context\n{context}")

    # Character info
    if bio_guidance:
        sections.append(f"**About you:** {bio_guidance}")

    # Commitment instructions (always included)
    action_instructions = build_owl_mail_commitment_instructions(PLAYER_NAME)
    sections.append(
        f"**Current date/time:** Wednesday, January 15th, 1891 2:30 PM\n\n"
        f"{action_instructions}"
    )

    # Writing instructions
    sections.append(f"## Instructions\n{LETTER_INSTRUCTIONS}")

    return "\n\n".join(sections)


# ---------------------------------------------------------------------------
# Judge criteria — two variants
# ---------------------------------------------------------------------------

CRITERIA_SHOULD_TAG = (
    "This letter was generated in response to a scenario where the NPC should propose "
    "or accept a meeting with the player. Evaluate:\n\n"
    "1. **Tag present**: The letter MUST contain an `[Action: Meet ...]` tag. "
    "Absence of the tag is an automatic 'weak'.\n\n"
    "2. **Format correctness**: The tag must match the exact format:\n"
    '   `[Action: Meet "Name" at "Location" on "M/D/YYYY H:MM AM/PM"]`\n'
    "   - Name must be the player's name in quotes\n"
    "   - Location must be in quotes and must be one of the available locations listed in the prompt\n"
    "   - Date/time must be in quotes in M/D/YYYY H:MM AM/PM format\n"
    "   - All three parts (name, location, datetime) must be present\n"
    "   A tag with wrong format (missing quotes, wrong date format, missing fields) is a demerit.\n\n"
    "3. **Coherence**: The tag's location and time should match what the letter says. "
    "If the letter says 'meet at the Three Broomsticks at 7' but the tag says "
    "Hog's Head at 3 PM, that's a problem.\n\n"
    "4. **Natural integration**: The action tag should appear naturally, not awkwardly "
    "bolted on. The letter itself should mention the proposed meeting.\n\n"
    "Rate 'perfect' if the tag is present, correctly formatted, coherent with the letter, "
    "and naturally integrated. 'weak' if tag is missing or fundamentally broken."
)

CRITERIA_SHOULD_NOT_TAG = (
    "This letter was generated in response to a scenario where NO meeting is being "
    "proposed or accepted. The letter should NOT contain any `[Action: Meet ...]` tag. "
    "Evaluate:\n\n"
    "1. **No action tag**: The letter must NOT contain `[Action: Meet ...]` or "
    "`[Action: CancelCommitment ...]`. Presence of an action tag is an automatic 'weak'.\n\n"
    "2. **Natural letter**: The letter should read naturally as a normal piece of "
    "correspondence — no forced meeting proposals where none was warranted.\n\n"
    "3. **In-character**: The letter should feel in-character for the NPC.\n\n"
    "Rate 'perfect' if there is no action tag and the letter reads naturally. "
    "'weak' if an action tag is present."
)


# ---------------------------------------------------------------------------
# Test cases — should use action tag
# ---------------------------------------------------------------------------

CASES = [
    # --- SHOULD produce an action tag ---
    JudgeEvalCase(
        input=build_letter_prompt(
            npc_display="Natsai Onai",
            is_reply=True,
            player_letter_subject="Feldcroft",
            player_letter_body=(
                "Natty, I've been thinking about what you said regarding Rookwood's people "
                "near Feldcroft. I think we should go investigate together. Want to meet at "
                "the Three Broomsticks tomorrow afternoon to plan?"
            ),
            context=(
                "Previous letters:\n"
                "- From Natsai Onai: A Warning — Overheard Sharp and Weasley discussing Rookwood activity near Feldcroft.\n"
                "----- Last Letter Correspondence Ends Here -----"
            ),
            bio_guidance="Brave Gryffindor student, fiercely loyal. Daughter of a renowned Seer.",
        ),
        criteria=CRITERIA_SHOULD_TAG,
        label="Reply accepting player's meeting proposal",
        min_rating="strong",
    ),
    JudgeEvalCase(
        input=build_letter_prompt(
            npc_display="Poppy Sweeting",
            context=(
                "Recent conversation:\n"
                f"- Poppy Sweeting: I found a hidden grove with a golden snidget! We should set up a sanctuary.\n"
                f"- {PLAYER_NAME}: That sounds wonderful, I'd love to help!\n"
                f"- Poppy Sweeting: Brilliant! I'll send you an owl with the details."
            ),
            bio_guidance="Kind Hufflepuff student, passionate about magical creatures and their welfare.",
        ),
        criteria=CRITERIA_SHOULD_TAG,
        label="NPC follow-up proposing a meeting they promised to owl about",
        min_rating="strong",
    ),
    JudgeEvalCase(
        input=build_letter_prompt(
            npc_display="Sebastian Sallow",
            is_reply=True,
            player_letter_subject="The Undercroft",
            player_letter_body=(
                "Sebastian, I found something in the restricted section that might help Anne. "
                "Meet me in the Undercroft tonight?"
            ),
            context=(
                "Previous letters:\n"
                "- From Sebastian Sallow: About Anne — Asking for help finding a cure.\n"
                "----- Last Letter Correspondence Ends Here -----"
            ),
            bio_guidance="Ambitious Slytherin student, driven by desire to cure his twin sister Anne's curse.",
        ),
        criteria=CRITERIA_SHOULD_TAG,
        label="Reply accepting player's urgent meeting request",
        min_rating="strong",
    ),
    JudgeEvalCase(
        input=build_letter_prompt(
            npc_display="Amit Thakkar",
            context=(
                "Recent conversation:\n"
                f"- Amit Thakkar: The conjunction of Jupiter and Saturn is this week!\n"
                f"- {PLAYER_NAME}: Oh, when exactly? I'd love to see it.\n"
                f"- Amit Thakkar: Thursday evening! I'll write you with the details."
            ),
            bio_guidance="Ravenclaw student, passionate astronomer. Enthusiastic but sometimes nervous.",
        ),
        criteria=CRITERIA_SHOULD_TAG,
        label="NPC initiating a planned stargazing meeting",
        min_rating="strong",
    ),

    # --- SHOULD NOT produce an action tag ---
    JudgeEvalCase(
        input=build_letter_prompt(
            npc_display="Garreth Weasley",
            is_reply=True,
            player_letter_subject="Potions ingredients",
            player_letter_body=(
                "Garreth, I found those dried billywig stings you needed. I left them "
                "on your desk in the Potions classroom."
            ),
            context=(
                "Previous letters:\n"
                "- From Garreth Weasley: A Favour — Asking for dried billywig stings for a brew.\n"
                "----- Last Letter Correspondence Ends Here -----"
            ),
            bio_guidance="Gryffindor student, enthusiastic and reckless potion experimenter.",
        ),
        criteria=CRITERIA_SHOULD_NOT_TAG,
        label="Reply thanking player, no meeting involved",
        min_rating="strong",
    ),
    JudgeEvalCase(
        input=build_letter_prompt(
            npc_display="Ominis Gaunt",
            context=(
                "Recent conversation:\n"
                f"- Ominis Gaunt: I saw Sebastian heading towards Feldcroft again.\n"
                f"- {PLAYER_NAME}: That's worrying. Should we say something?\n"
                f"- Ominis Gaunt: I'll think about it and write to you."
            ),
            bio_guidance="Slytherin student, blind, morally principled. Worried about Sebastian's dark magic obsession.",
        ),
        criteria=CRITERIA_SHOULD_NOT_TAG,
        label="NPC musing about a concern, no meeting proposed",
        min_rating="strong",
    ),
    JudgeEvalCase(
        input=build_letter_prompt(
            npc_display="Poppy Sweeting",
            is_reply=True,
            player_letter_subject="The Kneazles",
            player_letter_body=(
                "Poppy, how are the kneazles doing? I heard there was a storm "
                "near the den last night."
            ),
            context=(
                "Previous letters:\n"
                "- From Poppy Sweeting: Storm Warning — Worried about the kneazle den near the highlands.\n"
                "----- Last Letter Correspondence Ends Here -----"
            ),
            bio_guidance="Kind Hufflepuff student, passionate about magical creatures and their welfare.",
        ),
        criteria=CRITERIA_SHOULD_NOT_TAG,
        label="Reply about creature welfare, casual place mention only",
        min_rating="strong",
    ),
    JudgeEvalCase(
        input=build_letter_prompt(
            npc_display="Natsai Onai",
            context=(
                "Recent conversation:\n"
                f"- Natsai Onai: I stopped by the Three Broomsticks yesterday and overheard "
                f"something interesting about the poacher camps.\n"
                f"- {PLAYER_NAME}: What did you hear?\n"
                f"- Natsai Onai: I'll tell you more when I have the full picture."
            ),
            bio_guidance="Brave Gryffindor student, fiercely loyal. Daughter of a renowned Seer.",
        ),
        criteria=CRITERIA_SHOULD_NOT_TAG,
        label="NPC mentioning a location casually, not proposing to meet there",
        min_rating="strong",
    ),
    JudgeEvalCase(
        input=build_letter_prompt(
            npc_display="Sebastian Sallow",
            is_reply=True,
            player_letter_subject="How are you",
            player_letter_body=(
                "Sebastian, just checking in. How are things with Anne? "
                "I hope she's feeling better."
            ),
            context=(
                "Previous letters:\n"
                "- From Sebastian Sallow: Thanks — Thanking the player for their support.\n"
                "----- Last Letter Correspondence Ends Here -----"
            ),
            bio_guidance="Ambitious Slytherin student, driven by desire to cure his twin sister Anne's curse.",
        ),
        criteria=CRITERIA_SHOULD_NOT_TAG,
        label="Reply to a check-in letter, no meeting discussed",
        min_rating="strong",
    ),
    JudgeEvalCase(
        input=build_letter_prompt(
            npc_display="Garreth Weasley",
            context=(
                "Recent conversation:\n"
                f"- Garreth Weasley: I was experimenting in the Potions classroom and accidentally "
                f"melted Professor Sharp's favourite cauldron.\n"
                f"- {PLAYER_NAME}: Oh no, what did he say?\n"
                f"- Garreth Weasley: He hasn't noticed yet! I need to fix it before class tomorrow."
            ),
            bio_guidance="Gryffindor student, enthusiastic and reckless potion experimenter.",
        ),
        criteria=CRITERIA_SHOULD_NOT_TAG,
        label="NPC telling a story about a location, not inviting player",
        min_rating="strong",
    ),
    JudgeEvalCase(
        input=build_letter_prompt(
            npc_display="Amit Thakkar",
            is_reply=True,
            player_letter_subject="Your star charts",
            player_letter_body=(
                "Amit, your star charts from last week were brilliant. "
                "Professor Shah mentioned them in class today."
            ),
            context="(no recent interaction)",
            bio_guidance="Ravenclaw student, passionate astronomer. Enthusiastic but sometimes nervous.",
        ),
        criteria=CRITERIA_SHOULD_NOT_TAG,
        label="Reply to a compliment, no meeting context",
        min_rating="strong",
    ),
]


# ---------------------------------------------------------------------------
# Eval runner
# ---------------------------------------------------------------------------

def evaluate(case, model):
    result = llm.chat_simple(case.input, model=model, temperature=0.7, max_tokens=1024, context="eval")
    if not result:
        return ""

    # Show the generated letter
    print(f"           LETTER:")
    for line in result.strip().split("\n"):
        print(f"             {line}")
    print()

    return result


if __name__ == "__main__":
    settings = load_settings()
    conv = settings.get("conversation", {})
    run_judge_eval("Owl Mail Commitment Tags", CASES, evaluate, default_models=[
        conv.get("chat_model", "google/gemini-2.5-flash"),
    ], judge_model="x-ai/grok-4.1-fast")
