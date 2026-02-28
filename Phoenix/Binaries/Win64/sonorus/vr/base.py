"""
VR backend contract and shared data types.
"""
from abc import ABC, abstractmethod
from typing import Tuple, Optional


class PoseData:
    """Device pose snapshot."""
    __slots__ = ('yaw', 'pitch', 'position', 'valid')

    def __init__(self, yaw: float = 0.0, pitch: float = 0.0,
                 position: Tuple[float, float, float] = (0.0, 0.0, 0.0),
                 valid: bool = False):
        self.yaw = yaw
        self.pitch = pitch
        self.position = position
        self.valid = valid


class VRBackend(ABC):
    """Abstract base class for VR tracking backends."""

    name: str = "Unknown"

    @staticmethod
    @abstractmethod
    def is_available() -> bool:
        """Check if the backend library can be imported."""
        ...

    @abstractmethod
    def is_runtime_ready(self) -> bool:
        """Check if the VR runtime process is running."""
        ...

    @abstractmethod
    def init(self) -> bool:
        """Initialize the VR runtime and find devices."""
        ...

    @abstractmethod
    def poll(self) -> Tuple[PoseData, Optional[PoseData]]:
        """Poll device poses. Returns (hmd, right_controller_or_None)."""
        ...

    @abstractmethod
    def get_debug_info(self) -> Optional[dict]:
        """Get all device orientations for debugging."""
        ...

    @abstractmethod
    def shutdown(self) -> None:
        """Clean up runtime resources."""
        ...
