"""
LLM-as-judge eval harness for qualitative prompt evaluation.

Instead of exact-match pass/fail, a judge LLM rates each eval output on a
four-point scale (weak, okay, strong, perfect) and provides reasoning.

Usage from any eval script:
    from run_judge_eval import JudgeEvalCase, run_judge_eval

Run individual evals with:
    sonorus\\python\\python.exe sonorus\\evals\\<eval_name>.py [--model MODEL_ID] [--judge JUDGE_MODEL_ID]
"""

import argparse
import dataclasses
import os
import sys
import time

# Bootstrap sonorus imports
_script_dir = os.path.dirname(os.path.abspath(__file__))
_sonorus_dir = os.path.dirname(_script_dir)
if _sonorus_dir not in sys.path:
    sys.path.insert(0, _sonorus_dir)

from utils.settings import load_settings

# Populate model capabilities cache
try:
    import llm as _llm
    _llm.fetch_model_capabilities()
except Exception:
    pass

RATINGS = ["weak", "okay", "strong", "perfect"]
RATING_VALUES = {r: i for i, r in enumerate(RATINGS)}

JUDGE_SYSTEM = """You are an expert evaluator. You will be given a prompt that was sent to an LLM, the LLM's response, and specific criteria to judge the response against.

Rate the response using exactly one of these ratings:
- weak: Fails the criteria or has major problems
- okay: Partially meets criteria but has notable issues
- strong: Meets criteria well with only minor issues
- perfect: Fully meets all criteria with no issues

You MUST structure your response exactly like this:

Reasoning: <your detailed analysis of how well the response meets each criterion>
Rating: <weak|okay|strong|perfect>"""

JUDGE_PROMPT = """## Prompt sent to the LLM
{prompt}

## LLM's response
{response}

## Criteria
{criteria}

Evaluate the response against the criteria above. Think through each criterion carefully before giving your rating."""


@dataclasses.dataclass
class JudgeEvalCase:
    input: str
    criteria: str
    label: str = ""
    min_rating: str = "okay"


def _parse_judge_response(text):
    """Extract rating and reasoning from judge response."""
    text = text.strip()
    reasoning = ""
    rating = None

    for line in text.split("\n"):
        line_stripped = line.strip()
        lower = line_stripped.lower()

        if lower.startswith("reasoning:"):
            reasoning = line_stripped[len("reasoning:"):].strip()
        elif lower.startswith("rating:"):
            value = line_stripped[len("rating:"):].strip().lower().rstrip(".")
            if value in RATING_VALUES:
                rating = value

    # If we didn't find a structured rating, check the last line
    if not rating:
        last_line = text.strip().split("\n")[-1].strip().lower().rstrip(".")
        if last_line in RATING_VALUES:
            rating = last_line

    # Grab all text before the Rating: line as reasoning if we didn't parse one
    if not reasoning and "rating:" in text.lower():
        idx = text.lower().rfind("rating:")
        reasoning = text[:idx].strip()
        if reasoning.lower().startswith("reasoning:"):
            reasoning = reasoning[len("reasoning:"):].strip()

    return rating, reasoning


def run_judge_eval(name, cases, eval_fn, default_models=None, judge_model=None):
    """
    Run a judge-based eval suite across one or more models.

    Args:
        name:           Display name for the eval
        cases:          List of JudgeEvalCase
        eval_fn:        Callable(case, model) -> str (the LLM's output to judge)
        default_models: List of model IDs to test
        judge_model:    Model ID for the judge (defaults to settings or Opus)
    """
    import llm

    parser = argparse.ArgumentParser(description=name)
    parser.add_argument("--model", type=str, help="Test a specific model ID")
    parser.add_argument("--judge", type=str, help="Judge model ID")
    args = parser.parse_args()

    settings = load_settings()
    conv = settings.get("conversation", {})

    if args.model:
        models = [args.model]
    elif default_models:
        models = list(default_models)
    else:
        models = [conv.get("chat_model", "google/gemini-2.5-flash")]

    if args.judge:
        judge = args.judge
    elif judge_model:
        judge = judge_model
    else:
        judge = conv.get("judge_model", "anthropic/claude-sonnet-4")

    # Deduplicate while preserving order
    seen = set()
    unique_models = []
    for m in models:
        if m not in seen:
            seen.add(m)
            unique_models.append(m)
    models = unique_models

    print(f"\n{'=' * 60}")
    print(f"  {name}  (judge: {judge})")
    print(f"  {len(cases)} cases, {len(models)} model(s)")
    print(f"  Ratings: {' < '.join(RATINGS)}")
    print(f"{'=' * 60}")

    for model in models:
        print(f"\nModel: {model}")
        print("-" * 60)

        passes = 0
        failures = []
        ratings_count = {r: 0 for r in RATINGS}
        errors = 0
        start = time.time()

        for case in cases:
            # Step 1: Get the LLM's response
            response = eval_fn(case, model)
            if not response:
                errors += 1
                label = f"[{case.label}] " if case.label else ""
                print(f"  ERROR  {label}eval_fn returned empty")
                continue

            # Step 2: Judge the response
            judge_prompt = JUDGE_PROMPT.format(
                prompt=case.input,
                response=response,
                criteria=case.criteria,
            )
            judge_result = llm.chat_simple(
                judge_prompt,
                system=JUDGE_SYSTEM,
                model=judge,
                temperature=0,
                max_tokens=1024,
                context="eval",
            )

            if not judge_result:
                errors += 1
                label = f"[{case.label}] " if case.label else ""
                print(f"  ERROR  {label}judge returned empty")
                continue

            rating, reasoning = _parse_judge_response(judge_result)
            if not rating:
                errors += 1
                label = f"[{case.label}] " if case.label else ""
                print(f"  ERROR  {label}could not parse judge rating")
                print(f"           judge said: {judge_result[:200]}")
                continue

            ratings_count[rating] += 1
            min_val = RATING_VALUES.get(case.min_rating, 1)
            actual_val = RATING_VALUES[rating]
            passed = actual_val >= min_val

            if passed:
                passes += 1
                status = f" {rating.upper():>7} "
            else:
                failures.append((case, rating, reasoning))
                status = f" {rating.upper():>7}!"

            label = f"[{case.label}] " if case.label else ""
            preview = case.input[:60] + ("..." if len(case.input) > 60 else "")
            print(f"  {status}  {label}\"{preview}\"")

            if not passed:
                print(f"           min={case.min_rating}, got={rating}")
            if reasoning:
                # Show first 120 chars of reasoning
                short = reasoning[:120] + ("..." if len(reasoning) > 120 else "")
                print(f"           {short}")

        elapsed = time.time() - start
        total = len(cases) - errors
        pct = (passes / total * 100) if total else 0

        print(f"\nResults: {passes}/{total} passed ({pct:.1f}%) in {elapsed:.1f}s")
        if errors:
            print(f"  Errors: {errors}")

        # Rating distribution
        dist = "  Distribution: " + ", ".join(
            f"{r}={ratings_count[r]}" for r in RATINGS if ratings_count[r]
        )
        print(dist)

        if failures:
            print(f"\nBelow minimum ({case.min_rating}):")
            for case, rating, reasoning in failures:
                label = f"[{case.label}] " if case.label else ""
                print(f"  - {label}\"{case.input[:80]}\" (got={rating})")

    print()
