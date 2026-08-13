#!/bin/sh
# Reaches Python directly, and replaces itself doing it. A shell that waits
# instead of execing holds the signal, and so does the ./run dispatcher, whose
# bash function behind the `time` builtin was measured swallowing SIGTERM
# outright: exit 137, no sweep, and a month of multi-gigabyte scratch left on a
# disk that has filled before.
#
# The umask is the image's own. publish.py pins 0644 on the charts but leaves
# charts.json, index.html and the previews to whatever it inherits, and a site
# whose charts are readable and whose index is not is harder to spot than one
# that is plainly broken.
umask 0022
exec "$CHARTS_PYTHON" /opt/charts/pipeline.py "$@"
