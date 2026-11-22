from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from PIL import Image as PILImage


class Image:
    """Owns a pixel copy; tools cannot change the source or leak metadata."""
    def __init__(self, pixels):
        self._pixels = PILImage.frombytes('RGB', pixels.size, pixels.convert('RGB').tobytes())

    def pixels(self):
        return self._pixels.copy()


class Geocoder(Protocol):
    def city(self, lat: float, lon: float) -> str: ...


class GeocoderUnavailable(Exception):
    pass


@dataclass(frozen=True)
class ToolContext:
    gps: tuple[float, float] | None
    geocoder: Geocoder | None
    now: datetime


@dataclass(frozen=True)
class Warning:
    code: str
    message: str


@dataclass(frozen=True)
class ToolResult:
    image: Image
    warnings: tuple[Warning, ...] = ()

    @staticmethod
    def ok(image, warnings=()):
        return ToolResult(image, tuple(warnings))


class Tool(Protocol):
    name: str
    def validate(self, params: dict) -> None: ...
    def run(self, image: Image, params: dict, ctx: ToolContext) -> ToolResult: ...
