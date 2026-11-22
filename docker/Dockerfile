FROM python:3.14-slim

ARG APP_UID=1000
ARG APP_GID=1000

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /tmp/tesse
COPY docker/vendor/ebinex_sdk-*.whl docker/vendor/ebinex-sdk.lock /tmp/sdk/
COPY pyproject.toml README.md setup.py MANIFEST.in ./
COPY tesse ./tesse
COPY catalog ./catalog
RUN set -eu; \
    wheel=$(sed -n 's/^wheel=//p' /tmp/sdk/ebinex-sdk.lock); \
    digest=$(sed -n 's/^sha256=//p' /tmp/sdk/ebinex-sdk.lock); \
    set -- /tmp/sdk/ebinex_sdk-*.whl; \
    test "$#" -eq 1; test "${1##*/}" = "$wheel"; \
    printf '%s  %s\n' "$digest" "$1" | sha256sum -c -; \
    python -m pip install --no-cache-dir "$1" .; \
    rm -r /tmp/tesse /tmp/sdk

RUN test "$APP_UID" -gt 0 && test "$APP_GID" -gt 0 \
    && groupadd --gid "$APP_GID" tesse \
    && useradd --uid "$APP_UID" --gid "$APP_GID" --home-dir /nonexistent --no-create-home tesse
USER ${APP_UID}:${APP_GID}
WORKDIR /app
ENTRYPOINT ["tesse"]
