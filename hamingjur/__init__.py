"""Hamingjur: terminal applications, graphics, and input for Python."""
# std imports
import platform as _platform

# isort: off
if _platform.system() == 'Windows':
    from hamingjur.win_terminal import Terminal
else:
    from hamingjur.terminal import Terminal  # type: ignore[assignment]

from hamingjur.line_editor import LineEditor, LineHistory

from . import graphics, kitty, protocols, silkie
from .imaging import Transfer, ImageSupport, PreparedImage, DrawnImage

__all__ = ('Terminal', 'LineEditor', 'LineHistory', 'graphics', 'kitty', 'protocols', 'silkie',
           'Transfer', 'ImageSupport', 'PreparedImage', 'DrawnImage')
__version__ = "0.1.3"
