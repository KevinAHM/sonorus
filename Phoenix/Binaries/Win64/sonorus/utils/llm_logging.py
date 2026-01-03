"""
LLM logging utilities for Sonorus.
Logs all LLM requests and responses to daily log files.
"""

import os
import time

from .settings import SONORUS_DIR

LOGS_DIR = os.path.join(SONORUS_DIR, "logs")


def get_llm_log_path():
    """Get date-based log file path, creating logs dir if needed"""
    os.makedirs(LOGS_DIR, exist_ok=True)
    date_str = time.strftime('%Y-%m-%d')
    return os.path.join(LOGS_DIR, f"llm_{date_str}.txt")


def log_llm(payload, response=None, error=None):
    """Log LLM request/response to file"""
    try:
        with open(get_llm_log_path(), 'a', encoding='utf-8') as f:
            f.write(f"\n{'='*60}\n")
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}]\n")
            f.write(f"{'='*60}\n\n")

            f.write("=== REQUEST ===\n")
            f.write(f"Model: {payload.get('model')}\n")
            f.write(f"Temperature: {payload.get('temperature')}\n")
            f.write(f"Max Tokens: {payload.get('max_tokens')}\n\n")

            for msg in payload.get('messages', []):
                f.write(f"--- {msg['role'].upper()} ---\n")
                f.write(f"{msg['content']}\n\n")

            if response:
                f.write("=== RESPONSE ===\n")
                f.write(f"{response}\n")
            elif error:
                f.write("=== ERROR ===\n")
                f.write(f"{error}\n")

            f.write("\n")
    except Exception as e:
        print(f"[LLM] Failed to write log: {e}")
