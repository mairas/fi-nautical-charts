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

# Without these, nicely() prints one warning into hour three of a ten-hour log
# and every step runs at normal priority. Yielding the CPU and the disk is the
# arrangement under which this job is allowed on a host with production
# neighbours, so losing it should stop a build, not a boat.
RUN command -v nice >/dev/null && command -v ionice >/dev/null

COPY --from=deps /opt/charts-venv /opt/charts-venv

WORKDIR /opt/charts
COPY pipeline.py traficom_dl.py strip_nodata.py downscale.py publish.py currency.py index_page.py preview.py ./
COPY --chmod=0755 docker-entrypoint.sh /usr/local/bin/charts-pipeline

ARG REVISION=unknown
LABEL org.opencontainers.image.revision=$REVISION

ENV CHARTS_PYTHON=/opt/charts-venv/bin/python \
    CHARTS_VERSION=$REVISION \
    PYTHONUNBUFFERED=1

ENTRYPOINT ["/usr/local/bin/charts-pipeline"]
