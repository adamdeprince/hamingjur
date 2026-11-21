"""Annotated desktop images with readable text on portable terminals."""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from PIL.Image import Image

from ..imaging import Transfer
from ..keyboard import Keystroke
from ..silkie import ImageAnnotation
from ..window import Size
from ._i18n import Message
from .layout import Canvas, Rect, Surface
from .widgets import Widget, key_name

if TYPE_CHECKING:
    from ..terminal import Terminal
    from .desktop import Application


class ImageView(Widget):
    """A raster widget whose description is available as text and Silkie metadata."""

    focusable = True

    def __init__(self, image: Image, annotation: ImageAnnotation, **options):
        """Set the source image and the text explaining what it communicates."""
        options.setdefault('height', 6)
        super().__init__(**options)
        self.annotation = annotation
        self._revision = 0
        self.image = image

    @property
    def image(self) -> Image:
        """Return the original raster, retained for terminal density changes."""
        return self._image

    @image.setter
    def image(self, source: Image) -> None:
        if not isinstance(source, Image):
            raise TypeError('ImageView requires a Pillow image')
        self._image = source
        self.refresh()

    def refresh(self) -> None:
        """Mark a modified source image for upload on the next frame."""
        self._revision += 1

    def draw(self, surface: Surface, focused: Widget | None = None) -> None:
        """Draw a caption and text fallback before offering the image to the compositor."""
        surface.fill()
        annotation = self.annotation
        caption = annotation.name or Message('Image')
        surface.write(0, 0, caption, 'focus' if focused is self else 'accent')
        text = annotation.description or annotation.details
        if text:
            for y, line in enumerate(text.split('\n', surface.height)[:surface.height - 1], 1):
                surface.write(0, y, line.expandtabs(), 'text')
        box = surface.bounds
        surface._claim_image(self, Rect(box.x, box.y + 1, box.width, box.height - 1))

    def on_key(self, key: Keystroke | str, app: Application) -> bool:
        """Open a scrollable description from the keyboard."""
        if key_name(key) == 'ENTER':
            app.review_image(self)
            return True
        return False


@dataclass(frozen=True)
class _RasterKey:
    bounds: Rect
    density: Size
    revision: int


@dataclass
class _Displayed:
    image_id: int
    raster: _RasterKey
    annotation: ImageAnnotation | None = None


class _DesktopImages:
    """Keep native placements aligned with fully visible text-cell rectangles."""

    # The toolkit allocates one image per widget, with one placement per image.
    placement_id = 1

    def __init__(self, terminal: Terminal):
        self.terminal = terminal
        self.visible: tuple[ImageView, ...] = ()
        self._native: bool | None = None
        self._annotations: bool | None = None
        self._shown: dict[ImageView, _Displayed] = {}
        self._failed: dict[ImageView, _RasterKey] = {}

    def _drop(self, widget: ImageView) -> None:
        shown = self._shown.pop(widget)
        graphics = self.terminal.graphics
        graphics.send(graphics.delete(shown.image_id, q=2))

    def clear(self) -> None:
        """Release visible native graphics and their placement annotations."""
        for widget in tuple(self._shown):
            self._drop(widget)
        self._failed.clear()
        self.visible = ()
        self._native = self._annotations = None

    def sync(self, canvas: Canvas) -> None:
        """Update visible graphics and leave their text in place when unavailable."""
        self.visible = tuple(sorted(canvas.images, key=lambda view: (
            canvas.images[view].y, canvas.images[view].x)))
        for widget in self._shown.keys() - canvas.images.keys():
            self._drop(widget)
        for widget in self._failed.keys() - canvas.images.keys():
            del self._failed[widget]
        term = self.terminal
        if not self.visible or not term.is_a_tty or canvas.appearance.ascii_only:
            for widget in tuple(self._shown):
                self._drop(widget)
            return
        if self._native is None:
            self._native = term.does_kitty_graphics()
        if not self._native:
            return
        cell = term.get_cell_size()
        if cell is None:
            for widget in tuple(self._shown):
                self._drop(widget)
            return
        # Painting the background can invalidate its graphic claim; the frame's
        # placement decisions were already made using the complete composited screen.
        for widget, bounds in tuple(canvas.images.items()):
            key = _RasterKey(bounds, cell, widget._revision)
            if self._sync_image(widget, key):
                canvas.surface(bounds).fill('window')

    def _sync_image(self, widget: ImageView, key: _RasterKey) -> bool:
        if self._failed.get(widget) == key:
            return False
        shown = self._shown.get(widget)
        if shown is None or shown.raster != key:
            shown = self._upload(widget, key, shown)
            if shown is None:
                return False
            self._shown[widget] = shown
            self._failed.pop(widget, None)
        if shown.annotation != widget.annotation:
            if self._annotations is None:
                self._annotations = self.terminal.accessibility.supported()
            if self._annotations:
                self.terminal.accessibility.set_image(shown.image_id, self.placement_id,
                                                      widget.annotation, wait=False)
            shown.annotation = widget.annotation
        return True

    def _upload(self, widget: ImageView, key: _RasterKey,
                current: _Displayed | None) -> _Displayed | None:
        graphics = self.terminal.graphics
        box, cell = key.bounds, key.density
        try:
            image = graphics.prepare(widget.image, width=box.width * cell.width,
                                     height=box.height * cell.height, match_display=False)
        except (RuntimeError, ValueError):
            if current is not None:
                self._drop(widget)
        else:
            image_id = current.image_id if current else graphics._allocate_image_id(None)
            try:
                with self.terminal.location(box.x, box.y):
                    graphics._upload(image, image_id, Transfer.DIRECT, automatic=False, timeout=1.0,
                                     placement_id=self.placement_id)
                return _Displayed(image_id, key)
            except (RuntimeError, TimeoutError):
                # The peer might have drawn the image before its reply was lost.
                graphics.send(graphics.delete(image_id, q=2))
                self._shown.pop(widget, None)
        self._failed[widget] = key
        return None
