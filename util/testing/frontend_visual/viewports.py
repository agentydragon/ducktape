"""Browser geometry shared by visual and real-server tests."""

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.async_api import ViewportSize


@dataclass(frozen=True)
class Viewport:
    width: int = 1200
    height: int = 800
    device_scale_factor: float = 1
    has_touch: bool = False

    @property
    def size(self) -> ViewportSize:
        return {"width": self.width, "height": self.height}


DESKTOP = Viewport(width=1200, height=900)
MOBILE = Viewport(width=412, height=915, device_scale_factor=2.625)
SMALL_MOBILE = Viewport(width=360, height=650, device_scale_factor=2.625)
MOBILE_TOUCH = Viewport(width=412, height=915, device_scale_factor=2.625, has_touch=True)
