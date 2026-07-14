"""
Voice Manifest Builder - Multi-language voice reference preparation tool.

A self-contained web tool for extracting, analyzing, and selecting
optimal voice samples for TTS voice cloning across game languages.
"""

from .routes import voice_manager_bp

__all__ = ['voice_manager_bp']
