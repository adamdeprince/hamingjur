Window and cell geometry
========================

Cells and pixels are different units. ``term.width`` and ``term.height`` are
columns and rows reported by the operating system or an in-band resize event.
``term.pixel_width`` and ``term.pixel_height`` are the corresponding pixel
measurements, which can be zero when the OS does not supply them.

Query physical pixels per cell directly:

.. code-block:: python

    from hamingjur import Terminal

    term = Terminal()
    with term.cbreak():
        cell = term.get_cell_size(timeout=1.0)
        if cell is not None:
            print(cell.width, cell.height)
        text_cells = term.window.size()
        text_pixels = term.window.size(pixels=True)
        outer_pixels = term.window.size(pixels=True, outer=True)

``Size`` has named ``width`` and ``height`` fields. Unanswered queries return
``None``. ``term.cell_width`` and ``term.cell_height`` are convenient cached
properties that return ``-1`` if unknown. The older
``get_cell_height_and_width()`` returns ``(height, width)`` instead.
Use ``force=True`` to refresh a cell query after changing fonts. Inline and signal
resize events invalidate cached pixel dimensions automatically. With
``notify_on_resize()``, both input readers return ``RESIZE_EVENT``; Unix uses a
scoped SIGWINCH wakeup pipe when inline reports are unavailable. Complete inline
pixel/character dimensions also supply cell size without another query. See
:doc:`images` for adaptive drawing and signal context requirements. Call
``term.invalidate_geometry()`` after a reconnection or an external change when
no resize reports are being consumed.

``term.window.screen_size(pixels=True)`` queries the available screen, while
``position()`` and ``state()`` query the window position and iconified state.
Not every terminal implements these XTWINOPS reports; none of these APIs
substitutes a guessed pixel size for an unanswered query.

Preferred size
--------------

Applications can send standard XTWINOPS requests in either cells or pixels:

.. code-block:: python

    term.request_size(columns=120, rows=40)
    # Alternatively:
    term.request_size(width=1200, height=800)

Omitted dimensions remain unchanged; zero requests the corresponding screen
size. Mixed units are rejected. A request does not change ``term.width`` or
``term.height`` to the requested values. Query ``window.size()`` or consume
resize events to learn the actual dimensions.

Kitty and Silkie explicitly reject the standard cell/pixel resize escape
sequences. Their OS-window sizing uses Kitty remote control, which requires
terminal permission:

.. code-block:: python

    with term.cbreak():
        term.remote_control.resize_window(width=120, height=40, unit='cells')

This explicitly uses remote control and targets the calling OS window. It raises
an error on a refused command or unanswered request. See :doc:`remote_control`
for transport and authentication requirements. A window manager may still choose
a different actual size.

Other window operations
-----------------------

``hamingjur.window.command(operation, *parameters)`` encodes the XTWINOPS family;
``term.window.send()`` writes it. This includes move, iconify/deiconify,
raise/lower, refresh, maximize/fullscreen requests, title-stack operations,
and queries. Implementations may ignore or deny individual operations.

.. code-block:: python

    with term.window.title_saved():
        term.send(term.set_window_title('Drawing'))
        # Both original window and icon titles are restored on exit.

Wire definitions are in the
`XTerm control-sequence reference <https://invisible-island.net/xterm/ctlseqs/ctlseqs.html>`_.
