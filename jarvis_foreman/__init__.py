"""Jarvis Foreman — open junior/senior escalate stack for Agent Chat.

Phase A: events, seats, policy, sensors, channels, bus.
Later phases plug Twilio/Retell/Voice without rewriting the core.
"""

from .config import ForemanConfig, load_config, save_config, default_config
from .events import FlagEvent, Severity, new_event_id
from .bus import ForemanBus

__all__ = [
    "ForemanConfig",
    "load_config",
    "save_config",
    "default_config",
    "FlagEvent",
    "Severity",
    "new_event_id",
    "ForemanBus",
]

__version__ = "0.1.0"
