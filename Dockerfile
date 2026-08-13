# The monthly generator, built on the host that runs it and never pushed anywhere.
#
# Dependencies are resolved once, here, and baked in, so the 01:00 run reaches no
# package index at all. That is the guarantee `uv run --locked` buys on the host,
# moved to build time and frozen into an artifact. uv itself does not survive
# into the image: its cache cannot be made read-only, so keeping it would mean a
# writable directory tied to whichever uid the run happens to use.

FROM python:3.13-slim@sha256:ffb752e139c0a19692a43af8d8523b274222dd68eebad5d583b45c2201c6e30a AS deps
COPY --from=ghcr.io/astral-sh/uv:0.11.23@sha256:d0a0a753ab981624b49c97abc98821c1c09f4ca69d1ef5cee69c501be3d88479 /uv /usr/local/bin/uv

WORKDIR /src
COPY pipeline.py traficom_dl.py strip_nodata.py downscale.py publish.py ./
COPY pipeline.py.lock traficom_dl.py.lock strip_nodata.py.lock downscale.py.lock publish.py.lock ./

# One environment for all five scripts, exported from all five lockfiles rather
# than installed per script: uv keys a PEP 723 environment on a hash of the
# script's path and will not share one, so five scripts that between them ask for
# pillow, numpy and scipy get five copies. --require-hashes turns a wheel that
# does not match the lock into a failed build instead of a published chart.
RUN for s in pipeline traficom_dl strip_nodata downscale publish; do \
      uv export --locked --script "$s.py" --format requirements.txt >> /tmp/locked.txt; \
    done \
 && uv venv /opt/charts-venv \
 && VIRTUAL_ENV=/opt/charts-venv uv pip install --require-hashes -r /tmp/locked.txt


FROM python:3.13-slim@sha256:ffb752e139c0a19692a43af8d8523b274222dd68eebad5d583b45c2201c6e30a

COPY --from=deps /opt/charts-venv /opt/charts-venv

WORKDIR /opt/charts
COPY pipeline.py traficom_dl.py strip_nodata.py downscale.py publish.py currency.py index_page.py preview.py ./
COPY --chmod=0755 docker-entrypoint.sh /usr/local/bin/charts-pipeline

ARG REVISION
LABEL org.opencontainers.image.revision=$REVISION

ENV CHARTS_PYTHON=/opt/charts-venv/bin/python \
    CHARTS_VERSION=$REVISION \
    PYTHONUNBUFFERED=1

# The image saying it can run the job, rather than a file elsewhere saying so.
# Everything above names something by text -- an interpreter path, eight scripts,
# two wrappers, a commit -- and text is not evidence. Reaching for each of them
# here turns "this image cannot run a step" into a build that stops, on whichever
# host built it, rather than a month that ends at 01:00 with nothing published.
#
# The three imports pull in the whole set the run reaches, so a file left out of
# the COPY above fails here rather than at the step that wanted it, hours in.
# nicely() only warns when nice and ionice are missing, and a warning in hour
# three of a ten-hour log ends the arrangement that lets this share a host.
RUN command -v nice >/dev/null && command -v ionice >/dev/null \
 && "$CHARTS_PYTHON" -c "import pipeline, traficom_dl, downscale" \
 && { [ -n "$CHARTS_VERSION" ] || { echo "pass --build-arg REVISION=<commit>: a chart set that cannot name the code that made it is what this argument exists to prevent" >&2; exit 1; }; }

ENTRYPOINT ["/usr/local/bin/charts-pipeline"]
