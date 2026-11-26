#!/bin/sh
# Find a working Python 3 (python3 / python / py -3) and run transcripts.py.
# On Windows, python3/python may be Microsoft Store stubs that fail; each
# candidate is checked before use. If none works, exit quietly so the
# session is never blocked by this hook.
SCRIPT="$(dirname "$0")/../skills/transcript-search/transcripts.py"
for cand in python3 python "py -3"; do
  if $cand -c "import sys, sqlite3; sys.exit(0 if sys.version_info >= (3, 8) else 1)" >/dev/null 2>&1; then
    exec $cand -I "$SCRIPT" "$@"
  fi
done
exit 0
