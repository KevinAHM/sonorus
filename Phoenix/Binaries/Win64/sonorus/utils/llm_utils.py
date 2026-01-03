"""
LLM orchestration utilities for Sonorus.
Handles LLM calls, logging, and response parsing.
"""

import re

from .settings import load_settings
from .llm_logging import log_llm, LOGS_DIR

# Import llm module from parent directory
import llm


def call_llm(prompt, user_input):
    """Call LLM via shared llm module"""
    settings = load_settings()
    conv_settings = settings.get('conversation', {})
    model = conv_settings.get('chat_model', 'google/gemini-3-flash-preview')
    max_tokens = conv_settings.get('max_tokens', 150)
    temperature = conv_settings.get('temperature', 1.0)

    print(f"[LLM] Model: {model}, Temp: {temperature}, MaxTokens: {max_tokens}")

    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": user_input}
    ]

    # llm.chat() handles logging internally
    result = llm.chat(messages, model=model, temperature=temperature, max_tokens=max_tokens, context="chat")

    if result:
        print(f"[LLM] Success!")
        return result
    else:
        return "I seem to be having trouble thinking..."


def parse_action(text):
    """Parse action from LLM response if explicitly provided"""
    # Look for [Action: X] format
    match = re.search(r'\[Action:\s*(\w+(?:\s+\w+)?)\]', text, re.IGNORECASE)
    if match:
        return match.group(1)
    return "None"


def strip_action_tag(text):
    """Remove action tag from response text"""
    return re.sub(r'\s*\[Action:\s*\w+(?:\s+\w+)?\]', '', text).strip()
