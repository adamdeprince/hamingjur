Terminal protocols and shell integration
========================================

Hamingjur's terminfo attributes and named APIs cover everyday terminal work.
The ``hamingjur.control`` module also encodes the underlying ECMA-48 control
families, without imposing fixed payload or parameter-count limits:

.. code-block:: python

    from hamingjur import Terminal, control

    term = Terminal()
    term.send(control.csi('h', 2004, private='?'))   # Bracketed paste mode
    term.send(control.csi('m', '4:3'))              # Curly underline
    term.send(control.osc(0, 'My application'))     # Window/icon title
    term.send(control.dcs('+q544e'))               # XTGETTCAP name query

Use named restoring contexts, such as ``term.bracketed_paste()``, where
available. The low-level encoders validate framing and reject embedded control
characters in string payloads. They do not discover support, manage an
application's protocol state, or promise that a terminal implements an operation.

``term.send(sequence_or_iterable)`` writes and flushes complete commands.
``term.query(sequence, response_regex, timeout=...)`` waits for a matching
reply, preserving unrelated input for ``inkey()``. All query and output calls
respect the terminal's stream and styling rules. A query timeout returns
``None``; it does not truncate a large reply.

Shell integration
-----------------

Report a working directory with a correctly escaped OSC 7 file URI, and mark
prompt, input, output, and command completion with OSC 133:

.. code-block:: python

    term.shell.working_directory('/home/me/my project', host='workstation')
    term.shell.mark('A')             # Prompt begins
    term.send('$ ')
    term.shell.mark('B')             # Command input begins
    term.shell.mark('C')             # Command output begins
    term.shell.mark('D', status=0)   # Command finished successfully

Working-directory reports do not change Python's current directory. Silkie
resets device/reporting modes at a shell prompt marker, so applications that
mark prompts and use those modes must enable them after the marker.

Kitten tools, emulator settings, and desktop window-manager actions are separate
from terminal escape protocols. Hamingjur does not bundle the kitten plugin
runtime, perform native GPU allocation, or implement rsync's delta algorithm.
It exposes graphics surface descriptors and file-transfer packets so callers
can use resources and data produced elsewhere. The :doc:`features` map describes
these distinctions for each protocol family.
