#!/usr/bin/env python3
from hamingjur import Terminal

term = Terminal()

print("Watch the cursor disappear, ")
with term.dec_modes_disabled(term.DecPrivateMode.DECTCEM):
    print("Cursor is hidden - working...")
    term.inkey(2)

print()
print("Cursor is back!")
