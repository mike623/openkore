#!/bin/sh
# Build OpenKore's XSTools on macOS. scons wants `python`; uv provides it (see bin/python).
set -e
here="$(cd "$(dirname "$0")" && pwd)"
cd "$here/.."
PATH="$here/bin:$PATH" make
