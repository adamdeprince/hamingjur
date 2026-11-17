Adaptive image drawing
======================

Install ``pip install '.[images]'`` to enable Pillow image preparation. The
ordinary graphics byte encoders continue to work without Pillow.

.. code-block:: python

    from PIL import Image
    from hamingjur import Terminal, Transfer

    term = Terminal()
    with Image.open('photo.png') as image, term.cbreak():
        available = term.graphics.transfers()
        result = term.graphics.draw(image, x=0, y=0, columns=40, rows=15)
        # Or select a negotiated medium explicitly:
        if Transfer.SHARED_MEMORY in available:
            result = term.graphics.draw(image, columns=40, rows=15,
                                        transfer=Transfer.SHARED_MEMORY)

``width``/``height`` specify physical pixels; ``columns``/``rows`` specify a cell
bounding box. Do not mix the two units. The image fits inside the box while
preserving its aspect ratio, without upscaling. With no size, it fits the current
viewport. Unknown cell/viewport pixels raise an error instead of inventing
geometry; explicit pixel dimensions remain usable. ``x``/``y`` are zero-based
cell coordinates; omit both to draw at the cursor. Drawing at coordinates saves
and restores the cursor.

The planner queries accepted formats and the display's color model and precision.
It resizes a copy, then matches declared grayscale, monochrome or individual RGB
channel widths. Missing precision remains unknown. Set ``match_display=False``
to preserve source color precision. Transparency is retained even on gray
displays. Display matching and spatial downsampling can reduce information;
all subsequent encoding transforms preserve the resulting pixels exactly.

The library compares raw RGB/RGBA, PNG, exact PNG palettes, packed grayscale
and RGB565 where supported. Grayscale packing uses the lowest accepted precision
that represents every resulting intensity exactly, with separate byte padding
for each row. RGB565 is used only when expansion reproduces every channel.
Fast zlib compression is compared with the uncompressed representation, counting
base64 and protocol framing. There is no caller compression switch, JPEG encoding,
or additional approximate palette reduction. This selects among cheap lossless
candidates, rather than performing an exhaustive image optimization search.

For prepacked data, ``graphics.image()`` accepts grayscale formats
``301/302/304/308`` and little-endian RGB565 ``516``. Palette formats
``401/402/404/408`` carry one, two, four or eight bits per index: send all
``2 ** bits`` palette slots as straight RGBA bytes, then indices packed most
significant first, with each row padded to a byte boundary. The payload size is
``4 * 2 ** bits + ((width * bits + 7) // 8) * height``. These palettes belong to
the image; formats ``8`` and ``16`` instead index the terminal's own palette,
with per-pixel alpha in ``16``. High precision ``316/632/664`` carry little-endian
16-bit gray, RGB10A2 and 16-bit RGBA respectively. All require width and height.
``term.graphics.supports(format)`` probes each format with a complete sample.

``term.graphics.prepare(image, ...)`` returns a ``PreparedImage`` with ``data``,
``format``, ``size``, ``compressed`` and ``wire_size``. Pass it to ``draw(plan)``
to send those prepared bytes. ``DrawnImage`` records the acknowledged image ID,
plan and transfer. Reuse ``image_id`` to replace an image on redraw; release it
with ``term.graphics.send(term.graphics.delete(image_id))``. Keep the original
PIL image when planning again for a changed display or window.

Transfer negotiation
--------------------

``term.graphics.negotiate()`` returns ``ImageSupport(formats, transfers)``.
``term.graphics.transfers()`` returns a frozen set of ``Transfer`` enum members:

* ``DIRECT`` sends base64 packets over the terminal connection.
* ``FILE`` uses a regular file and removes the library-owned file after acknowledgement.
* ``TEMPORARY_FILE`` allows the terminal to delete the temporary file after reading.
* ``SHARED_MEMORY`` uses an owned named shared-memory object.
* ``GPU_SURFACE`` reports advertised support for an external GPU descriptor; use
  ``gpu_surface()`` rather than passing a PIL image for this medium.

Negotiation probes actual file and shared-memory resources: terminal support
alone does not establish that a remote connection can read local resources.
Without aggregate discovery, a successful Kitty graphics probe establishes its
standard RGB, RGBA and PNG formats, then local resource probes select the media.
Successful results are cached per terminal. Pass ``force=True`` after reconnecting
or changing the relay; it also refreshes display discovery on the next preparation.

Automatic drawing prefers negotiated shared memory, then temporary or regular
files, using direct transfer when its complete packet is shorter than the actual
resource reference. Explicit media are honored or rejected if unavailable.
Local resources remain alive until acknowledgement and are cleaned on success,
terminal errors, timeouts and exceptions. POSIX shared memory is unlinked either
by the terminal or by the library. Actual payload lengths are sent separately
from OS allocation sizes, which may be rounded to memory pages.

Queries preserve unrelated input. Upload failure raises an error; a missing
acknowledgement raises ``TimeoutError`` and is never silently retried. Serialize
calls that share a terminal. No image-size or retained-resource-count cap is
imposed; allocation failures propagate. GPU allocation remains application-owned.

Resize and display changes
--------------------------

``term.graphics.adaptive()`` scopes resize and, where available, display-change
reporting. Both synchronous and asynchronous input update drawing geometry and
display precision before returning their events. Read events and redraw from the
original image:

.. code-block:: python

    with Image.open('photo.png') as image, term.cbreak(), term.graphics.adaptive():
        result = term.graphics.draw(image, x=0, y=0)
        while True:
            key = term.inkey()
            if key == 'q':
                break
            event = key.protocol_event
            if key.name == 'RESIZE_EVENT' or (event and event.protocol == 'silkie:display'):
                result = term.graphics.draw(image, image_id=result.image_id, x=0, y=0)
        term.graphics.send(term.graphics.delete(result.image_id))

``notify_on_resize()`` prefers inline reports. On Unix it falls back to a scoped
SIGWINCH handler and pipe that wakes blocking ``inkey()`` and ``async_inkey()``.
Enter this fallback on the main thread. Existing handlers are chained and restored;
nested contexts reuse the pipe. Signals report the current OS size and may coalesce,
as SIGWINCH itself does. They are kept separate from keyboard bytes, including
unfinished escape sequences. Windows retains its native console resize fallback.
Reporting contexts restore their previous modes on exit; image lifetime remains
explicitly controlled by the application.
