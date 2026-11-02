"""Plays a recorded sign-in CLI session from a fixture file, in a terminal.

Fixture lines:
  > text      print text and a newline     (\\e is ESC)
  >> text     print text without a newline
  ? VALUE     read a line (echoed back, like a remote terminal); if it is not
              VALUE print "error: invalid code" and exit 1
  <           read a line and ignore it (e.g. "Press Enter")
  ~ SECONDS   sleep
  ! CODE      exit with CODE
Every line read is appended to the file named by FAKE_CLI_INPUT_LOG, if set,
so tests can see exactly what was typed.
"""

import os
import sys
import time


def out(text, newline=True):
    text = text.replace("\\e", "\x1b")
    sys.stdout.write(text + ("\n" if newline else ""))
    sys.stdout.flush()


def read_line():
    line = sys.stdin.readline().rstrip("\r\n")
    log = os.environ.get("FAKE_CLI_INPUT_LOG")
    if log:
        with open(log, "a") as fh:
            fh.write(line + "\n")
    return line


def main(path):
    for raw in open(path, encoding="utf-8").read().splitlines():
        if raw.startswith(">> "):
            out(raw[3:], newline=False)
        elif raw.startswith(">"):
            out(raw[2:])
        elif raw.startswith("? "):
            line = read_line()
            out(line)  # a terminal echoes what you type
            if line != raw[2:]:
                out("error: invalid code")
                sys.exit(1)
        elif raw.startswith("<"):
            read_line()
        elif raw.startswith("~ "):
            time.sleep(float(raw[2:]))
        elif raw.startswith("! "):
            sys.exit(int(raw[2:]))
    sys.exit(0)


if __name__ == "__main__":
    main(sys.argv[1])
