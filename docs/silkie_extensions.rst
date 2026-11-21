Additional Silkie extensions
============================

These APIs follow Silkie's source specifications and the
`Silkie reference <https://silkie.goblinreactor.com/reference.html>`_. Discovery
lists ``depth.1`` and ``ssh-agent.1`` independently of hardware or permissions.

Spatial attributes
------------------

.. code-block:: python

    from hamingjur import Terminal, graphics, silkie

    term = Terminal()
    print(term.depth(-150)('near text'))          # Signed millidegrees
    print(term.depth(pixels=-0.25, phase=0.125)('fractional offsets'))
    term.send(silkie.depth(250))                  # Subsequent text/placements
    term.graphics.send(graphics.place(7))
    term.send(silkie.depth())                     # Reset depth and phase

Pixel depth is measured in reference pixels at 1920 pixels per eye; phase is
relative vertical separation in physical pixels. Depth in millidegrees is a
signed 32-bit integer; pixel attributes must be finite and representable in
binary32. Choose either millidegrees or pixel depth. Omitting an attribute
preserves it; omitting all attributes resets both. Callable ``term.depth()``
styles reset both after their text without resetting colors or fonts.

Use vector record fields ``depth``, ``depth_px`` and ``phase`` for the same
attributes on groups and individual drawables. Placements inherit current text
attributes; explicit vector attributes follow the scene's group inheritance.
Painter order ``z`` remains independent of stereo depth. User-selected rendering
limits apply; the library does not activate tracking hardware. Silkie currently
renders spatial output on macOS and retains attributes on Linux while drawing flat.
Native presentation actions remain accessible through the existing authorized
remote-control command, for example::

    term.remote_control.call('action', {'action': 'set_spatial_mode follow', 'self': True})

Native tabs, settings and profiles are terminal UI/configuration features rather
than additional escape protocols. Supported mappable actions use the same remote
control API and the terminal's existing permissions.

Display preferences
-------------------

Silkie reports the host desktop's light or dark preference over the terminal
stream, including over SSH. Read it from the ``DisplayEvent`` returned by
``term.display.query()``, or from a display report received by
``term.inkey().protocol_event``. Its ``appearance`` property is a
``DisplayAppearance`` enum with values ``light``, ``dark``, and ``no_preference``.
Missing and unrecognized values resolve to ``no_preference``. The original
fields remain available on the event. The preference describes the host
desktop, independently of the terminal's theme or pane background.

``DisplayEvent.eink_scheme`` is Silkie's effective light or dark palette on
an e-paper display. It returns ``None`` when the host omits the field.

``DisplayEvent.high_contrast`` and ``DisplayEvent.differentiate_without_color``
give Silkie's effective accessibility preferences. Both return ``False`` for
missing or unrecognized values. Silkie can derive these hints from the display
and desktop settings, and users can override them in ``silkie.conf``.
``DisplayEvent.languages`` is a tuple of host language tags in preference order.
An empty tuple means the host reported no usable tags, while ``('und',)``
means the host explicitly reported an undetermined language.

Use ``term.display.enabled()`` to receive reports when the display state or
these preferences change. The context restores the previous reporting mode
on exit. Silkie also issues a resize notification when these preferences
change, even if the dimensions stay the same. Programs can query again when
display events are unavailable. Hamingjur widgets handle both routes
automatically. See :doc:`ui`.

Display precision and packed pixels
-----------------------------------

Display replies and events retain ``red_bits``, ``green_bits``, ``blue_bits``,
``gray_bits`` and ``precision`` alongside legacy ``bits``. RGB565 output can report
``bits=0`` and widths ``5/6/5``. Missing or zero widths are unknown; older nonzero
``bits`` applies equally to active channels. ``precision=framebuffer`` describes
the framebuffer representation, not a calibrated measurement of the panel.

These output fields do not restrict accepted image source formats. Packed
grayscale ``f=301/302/304/308`` and little-endian RGB565 ``f=516`` are accepted by
``graphics.image()`` and probed by ``term.graphics.supports()``. Aggregate
capabilities expose all advertised formats in ``caps.formats``. See :doc:`images`
for PIL downsampling, exact packing and automatic compression.

Notification return routes
--------------------------

.. code-block:: python

    from hamingjur import kitty

    route = kitty.notification_route([('odin-seat', 'sessionA', 3)])
    term.kitty.send(kitty.notify('Build complete', identifier='build',
                                r=route, a='focus,report'))

Every multipart fragment retains the route. Activation events preserve the full
``event.notification_route`` as ``(component, instance, window)`` tuples, ordered
from application outward. ``event.payload`` is the selected button and
``event.fields['a']`` holds effective actions, including an empty value. The terminal
and multiplexers own focus restoration; the library does not focus another window
merely because it receives a reply. Capability and close replies also retain routes.
Applications inside a multiplexer can normally omit ``r`` and let the relay append
its own hop. Wrapped notification IDs should remain opaque.

SSH-agent packet transport
--------------------------

``term.ssh_agent`` and ``term.silkie.ssh_agent`` expose the same explicit channel.
Construction does not open sockets, start an agent or change ``SSH_AUTH_SOCK``.
The provider must permit forwarding; protocol discovery alone grants no access.

``query()`` returns a report. ``caps=0`` means no permitted provider; ``caps=1``
requires its generation token in ``g``. Request IDs, connection IDs and generation
tokens allow ASCII letters, digits, periods, underscores and hyphens without a
library length limit. An optional ``identifier`` supplies a request ID; otherwise
the library generates a UUID token.

``exchange(packet, connection=..., generation=..., expires=...)`` takes exactly
one complete SSH agent packet, including its four-byte big-endian length and
message type. ``expires`` is an unsigned 64-bit Unix millisecond deadline chosen
by the application. Replies match ID, connection and generation. ``AgentEvent.data``
decodes and validates the packet in the ``data`` field. Transport failures retain
the SSH_AGENT_FAILURE packet and ``error`` field. A wait timeout returns ``None``;
no signing request is retried.

A proxy must query before each packet, pin each local connection to its first
generation, and fail/close it when the provider changes or a request times out.
Use ``close(connection, generation)`` in cleanup and never reuse connection tokens.
Persistent connection tokens preserve OpenSSH session-binding constraints.
``silkie.agent_request()`` provides a pure encoder for applications that manage
their own event loop. This API transports packets; it does not implement a Unix
socket proxy or the SSH agent's request policy.

Image accessibility
-------------------

Silkie's ``a11y.1`` capability lets an application describe individual image
placements. This works over the terminal connection, including an SSH session.
After placing a raster, vector, or SVG image with nonzero image and placement
IDs, attach a plain-text annotation:

.. code-block:: python

    from hamingjur import Terminal, graphics, silkie

    term = Terminal()
    description = silkie.ImageAnnotation(
        name='Requests by day',
        description='Tuesday had the most requests.',
        details='Monday: 30\nTuesday: 60\nWednesday: 45',
        purpose='essential',
    )
    document = '<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"/>'
    with term.cbreak():
        if term.accessibility.supported():
            term.graphics.send(graphics.svg(document, image_id=17, p=3, q=2))
            reply = term.accessibility.set_image(17, 3, description)
            if reply is not None and reply.status != 'OK':
                raise RuntimeError(reply.status)

``term.graphics.draw(image, placement_id=3)`` can also create a named placement
when the terminal supports Kitty graphics. ``term.accessibility`` and
``term.silkie.accessibility`` refer to the same client. Replies retain their
correlation ID and status. A timeout returns ``None``. Use ``wait=False`` for
operations that need no acknowledgement, or use the pure encoders
``silkie.annotate_image()`` and ``silkie.clear_image_annotation()`` in a custom
output loop.

Purposes are ``essential``, ``informative``, and ``decorative``. The default
is unclassified. The library percent-encodes Unicode text, including embedded
colons and percent signs. Description and details can contain tabs and line
breaks. Updating an annotation replaces the entire description. Deleting a
placement or replacing its image data also removes the annotation.

Text accessibility
------------------

Silkie's ``a11y.2`` capability lets applications label text for screen readers,
hide decorative cell ranges, and request one-shot announcements. Check
``term.accessibility.supported(version=2)`` before using these operations.

.. code-block:: python

    with term.cbreak():
        if term.accessibility.supported(version=2):
            term.send([term.move_xy(0, 1), '>>'])
            term.accessibility.set_text(7, (2, 1), (2, 3),
                                        role='prompt', label='Search')

            # Call when the error occurs.
            term.accessibility.announce('Search failed', priority='assertive')
            term.accessibility.clear_text(7)

Range coordinates are one-based ``(row, column)`` pairs on the active screen.
The start is inclusive, the end is exclusive, and the end column may be one
past the last column. Roles are ``prompt``, ``status``, ``completion``, and
``decorative``. A label replaces the accessible text. Decorative ranges are
omitted from it. Ranges expire when their cells change, the pane is resized,
or the terminal switches screens. A new range replaces overlapping ranges.

Announcements default to ``polite`` priority. Send them when an event occurs.
The terminal can acknowledge with ``ENOTSUP`` if native announcements are
unavailable. Acknowledgements include the terminal's status, and a timeout
returns ``None``. Pass ``wait=False`` to send without waiting for a reply.
``silkie.annotate_text()``, ``silkie.clear_text_annotation()``, and
``silkie.announce()`` encode the same requests for custom event loops.

Pane magnification and contrast
-------------------------------

Silkie can change the reported pixel size without changing the cell grid
when magnifying a pane with raster graphics. Use ``notify_on_resize()`` or
``graphics.adaptive()`` and prepare a fresh raster from the original source
after a resize event. Retained vector and SVG images scale in Silkie.

For explicit commands over the terminal connection, use
``term.remote_control.magnify_pane()``,
``term.remote_control.review_images()``, and
``term.remote_control.set_minimum_contrast()``. These require remote control
permission on the terminal. ``magnify_pane()`` takes ``'in'``, ``'out'``,
``'reset'``, ``'caret'``, a pan direction, or a numeric scale with an optional
one-based row and column. ``set_minimum_contrast(None)`` restores the pane's
configured default. The desktop toolkit provides a portable image review
and high-contrast palette. See :doc:`ui`.

Speech and Voice Lock
---------------------

``term.speech`` and ``term.silkie.speech`` provide the same client for the
terminal host's ``speech.1`` protocol. Query it from cbreak mode to get a
``SpeechCapabilities`` report with ``available``, ``engine``, and
``pitch_range``. An unavailable extension returns ``None``, and a responding
host without a backend reports ``available=False``.

.. code-block:: python

    from hamingjur import Terminal, silkie

    term = Terminal()
    with term.cbreak():
        caps = term.speech.query()
        if caps is not None and caps.available:
            term.speech.set_style('comment', silkie.SpeechStyle(
                language='fr-CA', rate=-10, pitch=-25, split_caps=True))
            term.speech.queue('Ligne 17', options=silkie.SpeechStyle(language='fr-CA'))
            term.speech.silence(100, queued=True)
            term.speech.queue('Enregistrer le fichier', style='comment')
            term.speech.dispatch()

``SpeechStyle`` holds voice, language, rate, pitch, pitch range, volume,
punctuation and mixed-case word splitting. Named styles are scoped to the
pane. Per-utterance options override the fields they set. Rate, pitch and pitch
range accept integers from −100 to 100. Volume accepts integers from 0 to 100.
Punctuation accepts ``none``, ``some`` or ``all``. Voice names, languages and
sound names depend on the backend installed on the terminal host.

``speak()`` submits text for playback and ``character()`` echoes one Unicode
character. ``sound()`` plays a native audio cue and ``silence()`` inserts a
duration in milliseconds. Queued items play on ``dispatch()``.
``pause()`` and ``resume()`` control playback, ``stop()`` clears the pane's
speech, and ``reset()`` also clears its named styles. ``clear_style()`` removes
one named style.

Commands send once by default. Pass ``wait=True`` for a correlated
``SpeechAck`` with the terminal's status or ``None`` on timeout. A successful
acknowledgement means the host accepted the command. ``silkie.speech_command()``
encodes speech commands for a custom transport or event loop.
