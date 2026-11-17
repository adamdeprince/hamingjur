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
