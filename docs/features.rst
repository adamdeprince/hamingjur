Terminal features
=================

Hamingjur is an independent fork of Blessed. Its implementation and public modules
live entirely in the ``hamingjur`` namespace. It does not install or import a
``blessed`` package, so both distributions can coexist in an environment.
Graphics, tablet input, typography, and display control are built-in features.

The implementation uses the Python standard library in addition to Blessed's
existing dependencies. Adaptive PIL image drawing uses optional Pillow support;
see :doc:`images`. It does not run kittens, allocate GPU surfaces, or require native
graphics bindings. Remote control is a separate, explicit API described in
:doc:`remote_control`. Vector rasterization, text shaping,
font loading, and hardware access occur in the terminal. The tablet example also
provides a pure Python raster brush. GPU transport references a
surface already allocated by the application or another library.

Output and input
----------------

The functions in ``hamingjur.graphics``, ``hamingjur.kitty`` and
``hamingjur.silkie`` encode commands without performing I/O. Streaming functions
yield strings. Write them with ``term.graphics.send(frames)`` or
``term.kitty.send(frames)``. These writers respect ``term.does_styling`` and flush
after the last packet. Finish a chunked transmission before starting another
command on that protocol. Terminal instances and their streams require external
synchronization if shared between threads or concurrent tasks.

``inkey()`` and ``async_inkey()`` return the existing string-compatible
``Keystroke`` type. Its ``protocol_event`` property contains a structured Kitty or
Silkie message, or ``None`` for ordinary input. ``tablet`` is a shortcut for tablet
reports. Events retain the original sequence, all fields, and the payload. Fields
are strings; ``integer(name)`` and ``number(name)`` distinguish absent values from
zero. ``data`` decodes base64 payloads; use it only for messages whose protocol
defines the payload as base64. Kitty graphics errors and notification activations,
for example, contain plain text instead.

Incomplete recognized string messages remain buffered across input timeouts.
Unknown OSC 777 selectors with version 1 are available as events. Invalid frames
and unsupported versions become ``PROTOCOL_UNKNOWN`` events, never individual
keystrokes. Applications should ignore those events. Chunked clipboard, drag/drop,
and file transfers arrive packet by packet: stream their data to application-owned
storage or accumulate it as needed. Hamingjur neither executes file transfers nor
opens paths received from the terminal automatically.

Discovery and tablet input
--------------------------

.. code-block:: python

    from hamingjur import Terminal
    from hamingjur.silkie import TabletMode

    term = Terminal()
    with term.cbreak():
        caps = term.silkie.capabilities()
        if caps and caps.supports('tablet-pointer'):
            requested = TabletMode.TOOLS | TabletMode.TOUCH
            with term.tablet.enabled(requested) as granted:
                while granted:
                    key = term.inkey()
                    if key == 'q':
                        break
                    if key.tablet is not None:
                        event = key.tablet
                        # Handle device/phase/contact/tool fields as appropriate.
                        # position is physical pixels; pressure is 0..1 or None.
                        position = event.position
                        pressure = event.pressure

Capability discovery lists selectors, their maximum versions, graphics media and
formats. Query each selector for hardware-specific capabilities:
``term.tablet.query()`` and ``term.display.query()`` return full
reports. Only successful aggregate discovery is cached; pass ``force=True`` after
reconnecting. Per-selector queries always read fresh state.

``enabled(mode)`` reads the existing mode, sets the requested bits, reads back
what was granted, and restores the previous mode in ``finally``. A terminal may
refuse bits; the yielded integer is authoritative. Unavailable modes and read-back
timeouts raise ``RuntimeError``. Nested contexts restore the enclosing state.
Applications must leave these contexts before suspending and re-enter after
resuming; the library does not install job-control signal handlers. Explicit
``term.silkie.reset()`` disables all extension reporting modes.

Tablet reports cover tools, fingers, pads, device descriptions, tool actions,
estimated/predicted samples and accidental touches. ``TabletMode`` names every
defined reporting bit. Pressure and tilt helpers honor the ``valid`` bitmask;
other measurements, serial/device IDs, contact lifecycles and predictions remain
available in ``event.fields``. Screen positions are in physical pixels and tilt
is in degrees. Touchpad fingers instead expose ``touchpad_position`` in 0..1;
their raw units must not be mistaken for screen coordinates. ``timestamp`` is
seconds on the terminal's monotonic clock, including ``tu`` when present.

Run ``python bin/tablet_canvas.py --mode MODE`` inside Silkie. The drawing
example requires one of these modes:

* ``tiles`` rasterizes pressure-sensitive strokes in pure Python. Each image is
  exactly one terminal cell in pixel dimensions; only changed cells are sent.
* ``vectors`` sends vector stroke creation and append records from Python.
* ``ink`` gives Silkie a brush and a separate vector image. Silkie draws pen
  strokes locally without a round trip to Python. A down report with an ``ink``
  ID owns the entire stroke; Python does not draw those samples again. Fingers
  and pen strokes without an ``ink`` ID use application-driven vectors.

All modes use a pen-shaped image pointer with its hotspot at the nib, restore
its previous shape on exit, and support simultaneous contacts, final pen-up
samples, and cancellation. Ink mode checks that terminal ink and tool reporting
were granted. The canvas uses the window and cell dimensions at startup; restart
the example after changing the window size or font size. Press ``q`` or Escape
to exit. No Pillow, native graphics bindings, or external renderer is needed.

Raster cells are allocated as touched and active contact layers grow as needed.
Completed raster strokes merge into the canvas, releasing their contact layers.
Vector IDs use the protocol's unsigned 32-bit space and report exhaustion rather
than wrapping. Terminal ink and application fallback strokes use separate images
so they cannot collide; terminal ink gets its image's full element ID range.
Ink is disabled and the demo's images are freed when its drawing context exits.

Protocol coverage
-----------------

The tables below map the Kitty protocol index, Silkie reference, and the
VT/xterm foundation to Hamingjur APIs. A streaming encoder or generic command
channel exposes a wire protocol; it does not implement an entire application
such as a file-transfer manager or a kitten plugin. The lower-level command functions
deliberately use the
specifications' field names so all operations remain accessible, including fields
introduced by newer terminals. Encoding support does not imply that a particular
terminal, relay, display or input device implements a feature.

.. list-table:: VT/xterm terminal foundation
   :header-rows: 1
   :widths: 30 70

   * - Feature
     - API
   * - Cell, text-area, outer-window and screen dimensions
     - ``get_cell_size()``, ``cell_width``, ``cell_height``, ``window.size()``,
       ``window.screen_size()``; see :doc:`window`
   * - Size preferences, window operations and title stack
     - ``request_size()``, ``window.send()``, ``window.title_saved()``;
       requests depend on terminal support and permissions
   * - Cursor movement, erasure, scrolling, tabs and terminfo capabilities
     - ``move_xy()``, ``location()``, ``scroll_region()``, terminfo attributes
   * - Attributes, colors, Unicode layout, links and text sizing
     - Formatting attributes, ``color_rgb()``, ``link()``, ``text_sized()``
   * - Screen, input and reporting modes
     - ``fullscreen()``, ``cbreak()``, ``raw()``, ``dec_modes_enabled()``,
       ``notify_on_resize()``, ``mouse_enabled()``, ``focus_events()``, ``bracketed_paste()``
   * - Device, software, capabilities and runtime state
     - ``get_device_attributes()``, ``get_software_version()``, ``get_xtgettcap()``,
       ``get_dec_mode()``, ``get_decrqss()``
   * - Clipboard and shell/application integration
     - ``clipboard_copy()``, ``clipboard_paste()``, ``set_window_title()``,
       ``progress_bar()``, ``shell.working_directory()``, ``shell.mark()``
   * - Kitty/Silkie OS-window sizing and remote commands
     - ``term.remote_control.resize_window()`` and ``call()``; explicit permission
       must be enabled in the terminal; see :doc:`remote_control`
   * - Other standard and future control sequences
     - ``hamingjur.control.csi/osc/dcs/apc``, ``term.send()``, ``term.query()``;
       see :doc:`terminal_protocols`

.. list-table:: Input, display, graphics, and typography
   :header-rows: 1
   :widths: 30 70

   * - Protocol
     - API
   * - Discovery and reset
     - ``term.silkie.capabilities()``, ``reset()``, ``channel(selector)``
   * - Tablet, touch, pads and tool actions
     - ``term.tablet``; ``label_button()``; ``key.tablet``
   * - Smooth scrolling and gestures
     - ``term.scroll``, ``term.gesture``
   * - Haptic feedback
     - ``term.haptic.send('play', pattern='alignment')``;
       ``start``/``stop`` and all device/target fields through the same channel
   * - Terminal-side ink
     - ``term.ink``; ``send('brush', i=..., k=..., n=..., w0=..., w1=...)``
   * - Font roles, weight and slant
     - ``term.silkie.select_font()``; ``font.command('state', id=...)``;
       ``font.send('reset')``
   * - Display, low latency and presentation timing
     - ``term.display``, ``latency`` and ``frame`` channels
   * - HDR, wide gamut and cell alpha
     - ``foreground_brightness()``, ``background_brightness()``,
       ``color_gamut()``, ``background_alpha()``; image keys ``G`` and ``T``
   * - Packed images and adaptive PIL drawing
     - ``graphics.image(format=301/302/304/308/516, ...)``; ``term.graphics.prepare()`` and
       ``draw()`` choose packing/compression; ``transfers()`` probes usable media
   * - Stereo depth and fractional vertical phase
     - ``term.depth()``; vector fields ``depth``, ``depth_px``, ``phase``;
       see :doc:`silkie_extensions`
   * - SSH-agent packets
     - ``term.ssh_agent.query()/exchange()/close()``; token IDs and provider generations
   * - Vector scenes, paths, gradients, groups, clips and ink
     - ``graphics.scene()``, ``patch()``, ``vector_record()``; ``f=200``
   * - Shaped/styled text, font resources and measurement
     - ``graphics.text_record()``; ``term.graphics.service()`` for ``fonts``,
       ``inspect``, ``upload``, ``release``, ``measure``;
       ``upload_font()`` and ``measure_text()`` conveniences
   * - HDR images and GPU surfaces
     - ``graphics.image(format=64, ...)``, ``term.graphics.gpu_surface()``;
       refresh with ``graphics.command(a='r', i=...)``
   * - Image-backed pointer shapes
     - ``kitty.pointer('image:9:24:24')`` and ``term.kitty.pointer()``

All named device and display channels offer ``command(operation, **fields)``,
``send(operation, **fields)`` and ``query()``. Channels with reporting modes
also support ``enabled()`` and ``read_mode()``.

.. list-table:: Kitty terminal protocols
   :header-rows: 1
   :widths: 30 70

   * - Protocol
     - API
   * - Graphics, all media and formats
     - ``graphics.transmit()``; ``image()``, ``place()``, ``delete()``;
       ``term.graphics.query()`` and ``supports()``
   * - Animation, composition, image numbers, usage hints and relative placement
     - ``graphics.transmit(a='f', ...)``; ``command(a='a', ...)``,
       ``command(a='c', ...)``; protocol keys including ``I``, ``N``, ``P/Q/H/V``
   * - Unicode image placeholders
     - ``place(..., U=1)`` and ``graphics.placeholder()``
   * - Keyboard and text sizing
     - ``enable_kitty_keyboard()`` and ``text_sized()``
   * - Styled/colored underlines and independent bold/faint reset
     - ``underline_style()``, ``underline_color()``, ``reset_bold()``, ``reset_faint()``
   * - Clipboard MIME data and paste events
     - ``kitty.clipboard_read()``, ``clipboard_write()``, ``clipboard_alias()``;
       ``kitty.packet(5522, ...)``; ``term.dec_modes_enabled(5522)``
   * - Drag and drop, drag images, remote files/directories
     - ``kitty.dnd()`` and ``dnd_data()`` with all ``t/x/y/X/Y/o/i`` fields;
       ``term.kitty.drops_enabled()``
   * - File transfer, compression and delta transport
     - ``kitty.file_transfer()`` and ``file_data()`` for every command;
       applications supply filesystem policy, zlib streams or rsync delta bytes
   * - Desktop notifications, icons, buttons, sounds and feedback
     - ``kitty.notify()``, ``notification()`` and ``notification_packet()``;
       repeated metadata keys are preserved by ``event.values(key)``
       and return hops by ``event.notification_route``
   * - Pointer shapes and stack
     - ``kitty.pointer()``; ``term.kitty.pointer()`` context
   * - Multiple cursors and queries
     - ``kitty.cursors()`` with shape, color and coordinate groups
   * - Color control and color stack
     - ``kitty.colors()`` and ``term.kitty.colors_saved()``
   * - Unscroll, region styling, mode save/restore and scrollback clearing
     - ``term.unscroll()``, ``kitty.region_style()``,
       ``term.kitty.modes_saved()``, ``term.clear_to_scrollback()``
   * - Mouse leave
     - ``MouseEvent.is_leave`` and ``MOUSE_LEAVE`` under SGR pixel reporting

Graphics examples
-----------------

.. code-block:: python

    from hamingjur import Terminal, graphics

    term = Terminal()
    if term.does_kitty_graphics():
        with open('photo.png', 'rb') as source:
            term.graphics.send(graphics.image(source, image_id=7, c=40, q=2))
        term.graphics.send(graphics.place(7, c=20, r=10, z=-1, q=2))
        term.graphics.send(graphics.delete(7, q=2))

Packed grayscale and RGB565 use formats 301/302/304/308 and 516. Raw RGB, RGBA and linear
little-endian half-float RGBA use formats 24, 32 and 64. All require width/height.
Palette images use 401/402/404/408; their payload starts with every RGBA palette
slot, followed by packed indices. Formats 8 and 16 index the terminal's palette.
PNG uses 100. ``compress=True`` on these byte encoders streams zlib compression;
the PIL drawing API chooses compression itself.
File, temporary-file and shared-memory transports take encoded resource names;
the terminal controls their documented deletion/unlink lifecycle. Query support
before sending Silkie-specific keys to Kitty.

.. code-block:: python

    if term.graphics.supports(200):
        records = [
            graphics.vector_record('g', id=1, box=(0, 0, 200, 100)),
            graphics.vector_record('p', id=2, g=1,
                                   d='M10,10L190,10L190,90L10,90Z', fill='#3388ff'),
            graphics.text_record(3, {'text': 'Hello', 'x': 20, 'y': 50,
                                     'size': 24, 'fill': '#ffffff'}, group=1),
        ]
        term.graphics.send(graphics.scene(records, image_id=8, width=200, height=100, q=2))
        term.graphics.send(graphics.patch(8, ['g id=1 t=2,0,0,2,0,0'], q=2))

Data retention and bounds
-------------------------

There is no library cap on total image size, number of vector records, response
length, queued reports, default editor input/history, undo history or killed-text
retention. Ordinary Python allocation failures propagate rather than truncating
data. Integer fields are checked against actual wire ranges. The remaining
4096-byte graphics/DnD packets, 4096-byte decoded clipboard/file packets,
2048-byte notification chunks and placeholder diacritic alphabet are protocol
constraints, not total-transfer limits. Individual OS read sizes and evictable
computation caches do not limit retained application data.

Specification references
------------------------

* `Kitty terminal extensions <https://sw.kovidgoyal.net/kitty/protocol-extensions/>`_
* `Kitty graphics <https://sw.kovidgoyal.net/kitty/graphics-protocol/>`_
* `Silkie extension reference <https://silkie.goblinreactor.com/reference.html>`_
* `Silkie tablet input <https://silkie.goblinreactor.com/tablet-protocol.html>`_
* `Silkie vector images <https://silkie.goblinreactor.com/vector-protocol.html>`_
