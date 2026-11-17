Remote control
==============

``term.remote_control`` sends Kitty/Silkie's JSON remote-control protocol in
pure Python. It never runs ``kitten``, reads a socket address from the environment,
or changes the terminal's permission settings. Construction and ordinary
terminal/graphics operations do not activate it.

The terminal must already permit remote control over the selected transport.
Its policy can refuse a request, and a window manager can restrict window sizing.

.. code-block:: python

    from hamingjur import Terminal

    term = Terminal()
    with term.cbreak():
        term.remote_control.resize_window(width=120, height=40, unit='cells')
        windows_json = term.remote_control.call('ls')

``resize_window()`` targets the calling OS window unless given an explicit
``match``. Zero leaves a dimension unchanged. With ``incremental=True``, positive
or negative values adjust existing dimensions. Successful acknowledgement means
the command was accepted, not that the window manager used the exact size.
Read ``term.window.size()`` or resize events to observe the result.

Socket transport
----------------

.. code-block:: python

    from hamingjur.remote_control import RemoteControl

    remote = RemoteControl(address='unix:/tmp/my-kitty-socket')
    remote.resize_window(width=1200, height=800, unit='pixels', match='id:7')

Unix sockets and ``tcp:host:port`` listeners are supported. Socket sizing requires
an explicit window match to avoid acting on an unrelated active window. Socket
reads accumulate until a complete frame arrives, with no total-response-size
limit. ``timeout`` applies to the whole socket exchange, including partial reads.
Normal Python allocation failures propagate without truncation.

Commands, responses, and limits
-------------------------------

``call(name, payload)`` accepts each command's documented JSON payload and returns
the response's ``data``. ``RemoteControlError`` retains a refused/failed command's
full response. Missing replies raise ``TimeoutError``. ``no_response=True`` is
available when explicitly desired, but cannot report a terminal refusal.

The default wire version is ``(0, 49, 2)``, matching the Kitty source against which
this client was developed. Construct ``RemoteControl(term, version=(...))`` for
an older version and use commands supported by that version.

``command()`` also accepts async/streaming envelope fields using their wire names,
for example ``**{'async': request_id}``. Applications own async cancellation and
streaming state. Late terminal replies arrive through
``inkey().protocol_event`` as ``remote-control`` events, whose payload is JSON.
Serialise synchronous calls sharing a terminal, since ordinary replies carry
no request correlation ID. ``term.query()`` preserves unrelated keyboard input.

Encrypted password authentication is not implemented by this standard-library
client. Plaintext passwords are rejected. Use a terminal-authorized transport,
or supply an envelope encrypted by an external authentication implementation to
``packet()`` and send it explicitly. Native kitten plugin execution is a separate
runtime and is not bundled with Hamingjur.

Wire definitions and the command catalog are documented in the
`Kitty remote-control protocol <https://sw.kovidgoyal.net/kitty/rc_protocol/>`_.
