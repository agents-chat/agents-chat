"""Pluggable sensors. Add a module + register in build_default_registry()."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .base import Sensor, Signal
from .bridge_health import BridgeHealthSensor
from .disk import DiskSensor
from .docker_containers import DockerContainersSensor
from .automation_pulse import AutomationPulseSensor
from .chat_activity import ChatActivitySensor
from .self_resources import SelfResourcesSensor
from .credentials import CredentialsSensor
from .degraded import DegradedSensor
from .backlog import BacklogSensor
from .cost_guard import CostGuardSensor
from .drift import DriftSensor
from .security import SecuritySensor
from .backups import BackupsSensor
from .memory_gap import MemoryGapSensor

if TYPE_CHECKING:
    from ..config import ForemanConfig


def build_default_registry(cfg: "ForemanConfig") -> list[Sensor]:
    # MemoryGapSensor remains importable for explicit diagnostics, but failed
    # retrieval telemetry is not an operator incident and must never enter the
    # default bus/narrator snapshot.
    sensors: list[Sensor] = [
        BridgeHealthSensor(cfg),
        DockerContainersSensor(cfg),
        AutomationPulseSensor(cfg),
        DiskSensor(cfg),
        ChatActivitySensor(cfg),
        SelfResourcesSensor(cfg),
        CredentialsSensor(cfg),
        DegradedSensor(cfg),
        BacklogSensor(cfg),
        CostGuardSensor(cfg),
        DriftSensor(cfg),
        SecuritySensor(cfg),
        BackupsSensor(cfg),
    ]
    return [s for s in sensors if s.enabled]


__all__ = [
    "Sensor",
    "Signal",
    "build_default_registry",
    "BridgeHealthSensor",
    "DockerContainersSensor",
    "AutomationPulseSensor",
    "DiskSensor",
    "ChatActivitySensor",
    "SelfResourcesSensor",
    "CredentialsSensor",
    "DegradedSensor",
    "BacklogSensor",
    "CostGuardSensor",
    "DriftSensor",
    "SecuritySensor",
    "BackupsSensor",
    "MemoryGapSensor",
]
