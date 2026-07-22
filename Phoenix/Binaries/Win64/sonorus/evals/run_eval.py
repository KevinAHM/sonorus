"""
Shared eval harness for running LLM prompt evaluations offline.

Usage from any eval script:
    from run_eval import EvalCase, run_eval

Run individual evals with:
    sonorus\\python\\python.exe sonorus\\evals\\<eval_name>.py [--model MODEL_ID]
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

# Populate model capabilities cache (reasoning support, etc.)
# Server does this at startup; evals must do it explicitly.
try:
    import llm as _llm
    _llm.fetch_model_capabilities()
except Exception:
    pass


@dataclasses.dataclass
class EvalCase:
    input: str
    expected: str
    label: str = ""


def run_eval(name, cases, eval_fn, default_models=None):
    """
    Run an eval suite across one or more models.

    Args:
        name:           Display name for the eval
        cases:          List of EvalCase
        eval_fn:        Callable(case, model) -> str (the LLM's normalized answer)
        default_models: List of model IDs to test. If None, pulls from settings.
    """
    parser = argparse.ArgumentParser(description=name)
    parser.add_argument("--model", type=str, help="Test a specific model ID")
    args = parser.parse_args()

    settings = load_settings()
    conv = settings.get("conversation", {})

    if args.model:
        models = [args.model]
    elif default_models:
        models = list(default_models)
    else:
        models = [conv.get("chat_model", "google/gemini-2.5-flash")]

    # Deduplicate while preserving order
    seen = set()
    unique_models = []
    for m in models:
        if m not in seen:
            seen.add(m)
            unique_models.append(m)
    models = unique_models

    print(f"\n{'=' * 60}")
    print(f"  {name}")
    print(f"  {len(cases)} cases, {len(models)} model(s)")
    print(f"{'=' * 60}")

    for model in models:
        print(f"\nModel: {model}")
        print("-" * 60)

        passes = 0
        failures = []
        start = time.time()

        for case in cases:
            result = eval_fn(case, model)
            passed = result.lower() == case.expected.lower()

            if passed:
                passes += 1
                status = " PASS "
            else:
                failures.append(case)
                status = " FAIL "

            label = f"[{case.label}] " if case.label else ""
            preview = case.input[:70] + ("..." if len(case.input) > 70 else "")
            print(f"  {status}  {label}\"{preview}\"")
            if not passed:
                print(f"           expected={case.expected}, got={result}")

        elapsed = time.time() - start
        total = len(cases)
        pct = (passes / total * 100) if total else 0
        print(f"\nResults: {passes}/{total} passed ({pct:.1f}%) in {elapsed:.1f}s")

        if failures:
            print(f"\nFailures:")
            for f in failures:
                label = f"[{f.label}] " if f.label else ""
                print(f"  - {label}\"{f.input[:80]}\" (expected={f.expected})")

    print()
