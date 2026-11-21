Hamingjur
=========

Hamingjur is a Python terminal library forked from
`Blessed <https://github.com/jquast/blessed>`_. It combines portable terminal
text and keyboard APIs with Kitty and Silkie graphics, tablet and touch input,
gestures, terminal-local ink, typography, HDR, clipboard, notifications, and
presentation timing.

Install with ``pip install hamingjur`` for the complete library, including image
drawing and vector rasterization. To install a checkout, use ``pip install .``.
All code lives in the
``hamingjur`` namespace; the package neither provides nor imports ``blessed``.
Hamingjur and upstream Blessed can be installed together.

.. code-block:: python

    from hamingjur import Terminal

    term = Terminal()
    print(term.bold('Hello from Hamingjur'))
    with term.cbreak():
        cell = term.get_cell_size()
        if cell is not None:
            print(f'Each cell is {cell.width} × {cell.height} physical pixels')

First-class terminal features
-----------------------------

* Text, colors, cursor movement, full-screen applications, Unicode layout,
  line editing, keyboard, mouse, focus, paste, and resize events.
* A terminal desktop with reusable widgets, overlapping windows,
  modal dialogs, menus, and configurable themes in ``hamingjur.ui``.
* Fixed-step terminal games with action bindings, cameras, sprite atlases and
  scrolling tile maps in ``hamingjur.game``. Run ``python -m bin.game_crossing``
  from a checkout to play inside a widget window.
* Raster and vector images, packed grayscale and RGB565, animations, image placement,
  styled vector text, font resources, HDR, wide gamut color, and image-backed pointers.
* Pen pressure and tilt, simultaneous contacts, tablet pads, gestures, scrolling,
  haptics, and terminal-local ink through ``term.tablet``, ``term.ink``, and related APIs.
* Window and cell geometry, size requests, display information, latency control,
  and frame presentation feedback.
* Clipboard formats, drag and drop, file-transfer packets, notifications,
  shell integration, and standard terminal control sequences.
* Stereo depth and fractional vertical phase for text and graphics, notification
  return routes, and explicit SSH-agent packet transport.

The `feature map <https://github.com/adamdeprince/hamingjur/blob/master/docs/features.rst>`_
describes the APIs, terminal requirements, and which operations use lower-level
protocol encoders. Terminal support and user permissions determine which requests
are accepted; a sent request does not prove that a terminal acted on it.

Open ``docs/hamingjur.html`` for a standalone project guide, working examples,
and a searchable API reference generated from the Python source. It works
offline. Rebuild it from the checkout with::

    python site/hamingjur-docs/build.py --export docs/hamingjur.html

Tablet drawing example
----------------------

Run inside Silkie, selecting one required mode::

    python bin/tablet_canvas.py --mode tiles
    python bin/tablet_canvas.py --mode vectors
    python bin/tablet_canvas.py --mode ink

``tiles`` paints into cell-sized raster images in Python. ``vectors`` sends
vector updates from Python. ``ink`` lets Silkie draw eligible pen strokes
locally, with Python fallback for other contacts. All three use a pen-shaped
pointer. Press ``q`` or Escape to exit.

Project and attribution
-----------------------

Homepage and API documentation: https://hamingjur.goblinreactor.com

Source and issues: https://github.com/adamdeprince/hamingjur

Hamingjur is an independent fork maintained by Adam DePrince, not a release of
Blessed. Fork contributions are copyright (c) 2026 Adam DePrince.

Hamingjur is distributed under the MIT license in ``LICENSE``. It retains the
inherited copyright notices: copyright (c) 2014 Jeff Quast and copyright (c) 2011
Erik Rose. Upstream contributors include Avram Lubkin.
Blessed was itself forked from `blessings <https://github.com/erikrose/blessings>`_.
The historical changelog records the upstream work on which this fork is based.
