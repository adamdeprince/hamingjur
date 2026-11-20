Hamingjur widgets
=================

``hamingjur.ui`` provides the Hamingjur widget library and terminal desktop for Python
applications. A desktop owns overlapping windows, a pull-down menu bar,
context menus and a status line. Each window owns its widget tree and remembers
keyboard focus. Dialogs temporarily own input, and controls can be used with
either the keyboard or a supported mouse. See the
`Turbo Vision project <https://github.com/magiblot/tvision>`_ for the original
interaction model.

Demo All
--------

Run the interactive widget demo from the checkout with Hamingjur's dependencies
installed.

.. code-block:: console

    $ python -m bin.ui_demo
    $ python -m bin.ui_demo --appearance ascii
    $ python -m bin.ui_demo --theme high-contrast
    $ python -m bin.ui_demo --theme my-palette.json

Choose a demo in the opening tree and press Enter. Use Left and Right to
collapse and expand branches, Ctrl+G to open the menus, and F1 for help with
the active window. The Demo menu reopens the launcher.

* **Controls.** Adjust the progress bar with the horizontal scrollbar. Submit
  a project name with Enter and recall a previous name with Ctrl+R. Tab or
  scroll down to the palette, checkboxes and Apply button. The Profile form
  demonstrates name and percentage validation and password masking.
* **Lists and text.** Type a station name prefix to select a report and
  press Enter to open it. Resize the split with the mouse or focus its
  divider and press Up or Down.
* **Layout and pointer.** Move the marker with the arrow keys or drag it.
  Press Space or right-click for its context menu. Resize the window to see
  the marker area stretch while the Center button stays anchored.
* **Workbench.** Edit the sample document, try Find and Replace All, and use
  the File menu to open, save, or choose a directory.
* **Images and themes.** Press Enter on the chart to read its description.
  Use the View menu to review visible images and the Edit menu to preview,
  apply or cancel theme changes.

Quick start
-----------

.. code-block:: python

    from hamingjur.ui import Application, Button, InputLine, Label, MenuItem
    from hamingjur.ui import Rect, Stack, StatusItem, Window

    app = Application()
    name = InputLine()

    def greet():
        app.set_status(f'Hello, {name.value}')

    app.add(Window('Greeting', Stack(
        Label('Name'), name, Button('Greet', greet), gap=1,
    ), Rect(4, 3, 40, 10)))
    app.menus.menus = (
        MenuItem.submenu('&File', MenuItem('&Quit', app.stop, 'Ctrl+Q')),
    )
    app.status_items = (StatusItem('Ctrl+G', 'Menu', app.menus.show),)
    app.run()

Use Tab and Shift+Tab to move between controls. Enter or Space activates a
button. F10 or Ctrl+G opens the menu, arrow keys navigate it, and Escape closes
it. An ampersand marks a menu mnemonic. For example, Alt+F opens ``&File`` on
terminals that report Alt. Use Ctrl+G and the arrow keys to choose the top-level
menu with an ordinary VT100 keyboard.

F6 cycles windows, F5 maximizes, and F7/F8 start keyboard move/resize. Provide
menu commands for ``next_window()``, ``maximize()``, and ``move_window()`` for
keyboards without those function keys. On mouse-capable terminals, drag a title
bar to move a window and drag its lower-right grip to resize it. The application
also exposes ``tile()``, ``cascade()``, and ``activate()``.

Color and terminal profiles
---------------------------

Themes set colors by role. Windows and controls share roles such as
``window``, ``bar``, ``focus``, ``selection``, and ``error``. By default, widgets
follow the light or dark desktop preference reported by the terminal. When the
terminal reports no preference, or the display protocol is unavailable, widgets
use the dark palette with slate backgrounds and mint accents. For e-paper,
widgets follow Silkie's effective e-paper palette. Older Silkie versions that
report e-paper without a palette use light.

Pass a theme to ``Application(theme=...)``, or assign one to ``app.theme``, to
keep a fixed palette. Pass ``None`` to resume following the terminal.
``ThemeDialog(app)`` offers a live preview and restores the previous choice on
Cancel.

.. code-block:: python

    from hamingjur.ui import Application, Theme

    app = Application(theme=Theme.from_mapping({
        'focus': {'fg': '#1A1124', 'bg': '#E0B1FF'},
        'bar': {'bg': '#2C213D'},
    }))

Use the same mapping as JSON with ``python -m bin.ui_demo --theme my-palette.json``.
Each role accepts ``fg`` and ``bg`` in ``#RRGGBB`` form, as well as ``bold``
and ``reverse`` booleans. Missing fields inherit the palette's defaults. Set
a color to JSON ``null`` to use the terminal's default color.

* VT100 uses ASCII frames and markers, bold and reverse video, and keyboard
  navigation. Unicode text is transliterated where it has an ASCII equivalent.
* xterm, iTerm2, Kitty, and Silkie use rounded Unicode frame corners.
  Kitty and Silkie also enable window shadows by default.
  Color depth, mouse reporting, and resize events use the terminal's capabilities.

Pass ``appearance=ASCII`` for ASCII, ``appearance=UNICODE`` for square Unicode
frames, or ``appearance=ROUNDED`` for rounded frames with shadows. If the terminal
maps ambiguous-width glyphs to two cells, automatic appearance uses ASCII chrome
with normal Unicode text.
Use the separate graphics API for retained SVG or its raster fallback.
See :doc:`images`.

Standard widgets
----------------

* **Layout and navigation.** ``Group`` and ``Stack`` compose widgets.
  ``Panel`` places widgets by cell rectangle with optional resize anchors.
  ``SplitView`` provides a draggable divider. ``ScrollView`` brings focused
  controls into view for forms larger than the terminal's work area.
* **Controls.** ``Label``, ``Button``, ``Checkbox``, ``CheckGroup``,
  ``RadioGroup``, ``InputLine``, ``ProgressBar``, and ``ScrollBar``.
  Input lines reuse Hamingjur's grapheme-aware editor and history. Ctrl+R opens
  the history picker. Input validators return an error string or ``None``.
* **Views.** ``ListBox`` has incremental search and scrolling, ``TreeView``
  expands and collapses an outline, and ``TextView`` scrolls a document.
  ``TextEditor`` adds multiline editing, selection, undo/redo, the desktop
  clipboard, search and replace, and file loading and saving.
* **Dialogs.** ``Dialog``, ``FormDialog``, ``message_box()``, ``prompt()``,
  ``FileDialog``, ``DirectoryDialog``, and ``ThemeDialog`` supply common
  interactions. File dialogs return a path to the application. ``HelpBook``
  ties F1 to the active window's help topic, with related pages and navigation.

A widget has local drawing coordinates in ``draw(surface, focused)`` and screen
coordinates in its ``bounds``. Override ``on_key()`` and ``on_pointer()`` to
accept input. Return true to consume the event. Use ``app.dispatch(event)``
and ``app.draw()`` if you already manage an event loop or want to exercise a
desktop without entering interactive terminal modes.

Accessibility
-------------

Use ``Theme.high_contrast()`` for strong foreground and background separation.
The terminal cursor tracks the focused editor's caret so Silkie's **Center on
Text Cursor** command follows the insertion point. Pane magnification keeps
the same cell layout. Raster widgets reuse their source image at the new pixel
density when the terminal sends a resize report.

``ImageView(image, annotation)`` accepts a Pillow image and a
``silkie.ImageAnnotation``. Give informative or essential graphics a short name
and describe the result they communicate. Enter on the focused widget opens the
full text. ``app.review_images()`` reviews fully visible image widgets and skips
decorative ones by default.

On Silkie with ``a11y.1``, the widget also publishes its description to the
terminal's native image review. Images and descriptions travel over the
terminal stream. Kitty displays the image and keeps its
description in the toolkit's own review. On text-only and sixel terminals,
the caption and summary render as text, so overlapping windows can still
hide and restore the view. Custom event loops can call
``app.release_images()`` before leaving the alternate screen.

API
---

.. automodule:: hamingjur.ui
   :members:
   :imported-members:
