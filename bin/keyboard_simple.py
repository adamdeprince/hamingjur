#!/usr/bin/env python3
from hamingjur import Terminal

term = Terminal()
with term.cbreak():
    key = term.inkey()
    print(f"You pressed: {key!r}")
