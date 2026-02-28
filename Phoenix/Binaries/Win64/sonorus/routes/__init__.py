"""
Sonorus Flask route blueprints.

Modular HTTP API endpoints extracted from server.py.
"""

from .setup import setup_bp
from .memory import memory_bp
from .dialogue import dialogue_bp
from .config import config_bp
from .commitments import commitments_bp

# Voice Manager is in its own package
from voice_manager import voice_manager_bp

__all__ = [
    'setup_bp',
    'memory_bp',
    'dialogue_bp',
    'config_bp',
    'commitments_bp',
    'voice_manager_bp',
]


def register_blueprints(app):
    """Register all route blueprints with the Flask app."""
    app.register_blueprint(setup_bp)
    app.register_blueprint(memory_bp)
    app.register_blueprint(dialogue_bp)
    app.register_blueprint(config_bp)
    app.register_blueprint(commitments_bp)
    app.register_blueprint(voice_manager_bp)
