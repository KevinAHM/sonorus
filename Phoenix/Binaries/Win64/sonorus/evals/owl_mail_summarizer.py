"""
Eval: Owl Mail Letter Summarizer

Tests whether the summarizer prompt produces good condensed versions of letters.
Judge evaluates on:
  - Compression: how much removable content was actually removed
  - Preservation: no important information lost
  - Perspective: maintains first-person voice of the letter author
  - Format: single paragraph, no bullet points or labels
"""

import os, sys
_evals_dir = os.path.dirname(os.path.abspath(__file__))
if _evals_dir not in sys.path:
    sys.path.insert(0, _evals_dir)

from run_judge_eval import JudgeEvalCase, run_judge_eval
import llm
from utils.settings import load_settings
import re

# --- Reconstruct the summarizer prompt (mirrors owl_orchestrator._summarize_letter) ---

def build_summarizer_prompt(sender_display, subject, body):
    return (
        f"Summarize this letter as a single concise paragraph.\n\n"
        f"## Rules\n"
        f"1. **Core content only**: Focus on key information — requests, proposals, reactions, decisions. Remove pleasantries and filler.\n"
        f"2. **Direct language**: Replace flowery phrasing with direct statements.\n"
        f"3. **Preserve perspective**: Write as the letter's author (first person), not as a third-party description.\n"
        f"4. **Preserve names**: Keep all character and location names exactly as written.\n"
        f"5. **Preserve meaning**: Accurately convey the intent of the original letter.\n"
        f"6. **Single paragraph**: No formatting, no bullet points.\n\n"
        f"## Letter\n"
        f"**From:** {sender_display}\n"
        f"**Subject:** {subject}\n\n"
        f"{body}\n\n"
        f"Output your summary inside <summary> tags."
    )

def parse_summary_tag(text):
    cleaned = text.strip()
    cleaned = re.sub(r'^```(?:xml)?\s*', '', cleaned)
    cleaned = re.sub(r'\s*```$', '', cleaned)
    cleaned = cleaned.strip()
    m = re.search(r'<summary>\s*(.*?)(?:</summary>)', cleaned, re.DOTALL)
    if m:
        return m.group(1).strip()
    m = re.search(r'<summary>\s*(.*)', cleaned, re.DOTALL)
    if m:
        return m.group(1).strip()
    return cleaned if cleaned else ""

# --- Test cases ---

JUDGE_CRITERIA = (
    "Evaluate this letter summary on four axes:\n\n"
    "1. **Compression**: How much removable content (pleasantries, filler, flowery language) was actually removed? "
    "Score higher when more unnecessary content is stripped. A summary that's barely shorter than the original is bad.\n\n"
    "2. **Preservation**: Was any important information lost? Requests, proposals, names, locations, key facts, "
    "emotional intent — all must survive. Losing important content is a strong demerit.\n\n"
    "3. **Perspective**: The summary must remain addressed to the recipient, as if still part of the letter. "
    "It should NOT narrate or describe what the letter says ('I'm asking them to...', 'the letter mentions'). "
    "It must read as the author speaking TO the recipient, not ABOUT the letter. Strong demerit if it becomes introspective or self-describing.\n\n"
    "4. **Format**: Is it a single unformatted paragraph? No bullet points, no labels, no headers.\n\n"
    "Rate 'perfect' if all four axes are strong. Rate 'weak' if important info is lost or perspective is wrong."
)

CASES = [
    JudgeEvalCase(
        input=build_summarizer_prompt(
            "Garreth Weasley", "A Favour to Ask",
            "I must say, Adri, that was certainly more information than I bargained for when I asked about the "
            "Honeydukes run! While I admire your dedication to total honesty, perhaps we can keep our future "
            "conversations focused on the more \"fragrant\" matters of potion-making instead. I trust that now "
            "you're back on your feet, you haven't forgotten about those dried billywig stings I need for my "
            "latest concoction. It's going to be a truly spectacular brew, provided I can get the fizz just "
            "right. Do stop by the Potions classroom once you've recovered from your—er—ordeal!"
        ),
        criteria=JUDGE_CRITERIA,
        label="Garreth — chatty with embedded request",
        min_rating="strong",
    ),
    JudgeEvalCase(
        input=build_summarizer_prompt(
            "Sebastian Sallow", "The Undercroft",
            "I need to talk to you about something. Meet me in the Undercroft tonight — and don't tell anyone. "
            "This isn't something I can put in a letter, but trust me, it's important. Come alone."
        ),
        criteria=JUDGE_CRITERIA,
        label="Sebastian — short and urgent, little to remove",
        min_rating="strong",
    ),
    JudgeEvalCase(
        input=build_summarizer_prompt(
            "Poppy Sweeting", "The Snidget Sanctuary",
            "Oh Adri, you won't believe what happened today! I was out near the highlands checking on the "
            "mooncalf dens — you know how I worry about them when the weather turns — and I stumbled upon "
            "the most extraordinary thing. There's a hidden grove past the old standing stones, completely "
            "sheltered from the wind, and I spotted what I'm almost certain was a golden snidget! They're "
            "incredibly rare, nearly extinct in fact. I've been reading everything I can find in the library "
            "about their habitats. I think if we're very careful, we could set up a small sanctuary there. "
            "Would you help me? I'd need someone to gather some fluxweed and dittany to plant around the "
            "perimeter — they're drawn to both. We should go this weekend before the frost sets in. "
            "I'm so excited I can barely sit still to write this!"
        ),
        criteria=JUDGE_CRITERIA,
        label="Poppy — long and excited, lots of filler",
        min_rating="strong",
    ),
    JudgeEvalCase(
        input=build_summarizer_prompt(
            "Natsai Onai", "A Warning",
            "I hope this letter finds you well, though I'm afraid I don't have the best news. I overheard "
            "Professor Sharp speaking with Professor Weasley in the faculty tower this morning. They mentioned "
            "Rookwood by name — something about increased activity near Feldcroft. Sharp looked concerned, "
            "which as you know is unusual for him. I think we should be careful about our next trip there. "
            "Sebastian won't want to hear this, but perhaps we should wait until we know more. I'll keep "
            "my ears open. Stay safe."
        ),
        criteria=JUDGE_CRITERIA,
        label="Natsai — important plot info mixed with pleasantries",
        min_rating="strong",
    ),
    JudgeEvalCase(
        input=build_summarizer_prompt(
            "Amit Thakkar", "Astronomy Tower",
            "Clear skies tonight! Perfect conditions for stargazing. Meet me at the Astronomy Tower at "
            "9 PM if you're free. I've been charting Jupiter's moons all week and tonight they should "
            "all be visible."
        ),
        criteria=JUDGE_CRITERIA,
        label="Amit — concise, almost nothing to remove",
        min_rating="strong",
    ),
    JudgeEvalCase(
        input=build_summarizer_prompt(
            "Deek", "Room of Requirement",
            "Deek is hoping you will come to the Room of Requirement when you have a moment, friend. "
            "Deek has been tending to the beasts as always, and they are all doing splendidly — the "
            "niffler especially has been in fine spirits, always digging about for shiny things, as "
            "nifflers do. But Deek has noticed something peculiar with the vivarium enchantments. "
            "The temperature charm on the swamp vivarium has been flickering, and Deek is worried "
            "the dugbogs might catch cold. Deek has tried adjusting it but Deek thinks it needs "
            "a proper wizard's touch. Also, the mooncalf pen could use some more moonstone — they've "
            "been looking rather dim lately. Deek would be most grateful for your help!"
        ),
        criteria=JUDGE_CRITERIA,
        label="Deek — third-person speech pattern, mixed requests",
        min_rating="strong",
    ),
    JudgeEvalCase(
        input=build_summarizer_prompt(
            "Ominis Gaunt", "Regarding Sebastian",
            "I'm writing because I don't know who else to turn to. Sebastian has been disappearing "
            "again — he missed Charms twice this week and when I confronted him about it he wouldn't "
            "say where he'd been. He got defensive, which you know is never a good sign with him. "
            "I can't shake the feeling he's gone back to studying that wretched relic. You and I both "
            "know where that path leads. I'm not asking you to spy on him, but if you notice anything "
            "unusual, please let me know. I worry about him more than I'd like to admit. Perhaps we "
            "could meet at the Quad Courtyard tomorrow afternoon to discuss this properly — I'd rather "
            "not put too much in writing."
        ),
        criteria=JUDGE_CRITERIA,
        label="Ominis — emotional with meeting proposal and key info",
        min_rating="strong",
    ),
]


def _extract_body(prompt):
    """Pull the letter body out of the summarizer prompt for display."""
    marker = "**Subject:**"
    idx = prompt.find(marker)
    if idx == -1:
        return ""
    # Skip past the subject line
    after = prompt[idx:]
    lines = after.split("\n", 2)
    if len(lines) >= 3:
        return lines[2].replace("\n\nOutput your summary inside <summary> tags.", "").strip()
    return ""


def evaluate(case, model):
    result = llm.chat_simple(case.input, model=model, temperature=0, max_tokens=256, context="eval")
    if not result:
        return ""
    summary = parse_summary_tag(result)

    # Show original vs summary for manual comparison
    original = _extract_body(case.input)
    print(f"           ORIGINAL ({len(original)} chars):")
    print(f"           {original}")
    print(f"           SUMMARY  ({len(summary)} chars, {100 - len(summary) * 100 // max(len(original), 1)}% reduction):")
    print(f"           {summary}")
    print()

    return summary


if __name__ == "__main__":
    settings = load_settings()
    run_judge_eval("Owl Mail Summarizer", CASES, evaluate, default_models=[
        "mistralai/mistral-small-3.2-24b-instruct",
    ], judge_model="x-ai/grok-4.1-fast")
