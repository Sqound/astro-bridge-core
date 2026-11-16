"""Internal camera contracts; importing the package opens no device or service."""
from .driver import CameraDriver, CameraError
from .frames import Frame

__all__ = ['CameraDriver', 'CameraError', 'Frame']
