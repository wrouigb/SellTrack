# syntax=docker/dockerfile:1

# Two images from one file, always released together under the same tag:
#   --target web     the app, the scheduler and the one-off jobs
#   --target nginx   Nginx, with the built CSS and JavaScript inside it

# The assets are the same on every platform, so they're only built once, natively. Under emulation
# this would take minutes on the way to a Raspberry Pi image.
FROM --platform=$BUILDPLATFORM node:22-slim AS assets
WORKDIR /build
COPY package.json package-lock.json ./
RUN npm ci
COPY scripts ./scripts
COPY app ./app
RUN npm run build


FROM python:3.13-slim AS dependencies
ENV UV_PROJECT_ENVIRONMENT=/opt/venv UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy
RUN pip install --no-cache-dir uv==0.11.32
WORKDIR /build
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --compile-bytecode


FROM python:3.13-slim AS web
ARG REVISION=unknown
LABEL org.opencontainers.image.title="SellTrack" \
      org.opencontainers.image.source="https://github.com/wrouigb/SellTrack" \
      org.opencontainers.image.revision="$REVISION"
# Without a real configuration it refuses to start, rather than starting with a weak one.
ENV APP_ENV=production \
    FLASK_APP=app:create_app \
    PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
# Security fixes from since the base image was built. Then out with what's only there for installing
# things, because nothing here installs anything once it's built, and neither should an intruder.
RUN apt-get update \
    && apt-get upgrade --yes \
    && rm -rf /var/lib/apt/lists/* \
    && pip uninstall --yes --root-user-action=ignore pip \
    && rm -rf /usr/local/lib/python3.13/ensurepip \
    && useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin selltrack
WORKDIR /srv/selltrack
COPY --from=dependencies /opt/venv /opt/venv
COPY app ./app
COPY migrations ./migrations
COPY deploy ./deploy
COPY --from=assets /build/app/static/dist ./app/static/dist
# The file system is read-only when running, so anything compiled later could never be kept.
RUN python -m compileall -q app migrations
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request as r; r.urlopen('http://127.0.0.1:8000/healthz', timeout=2)"]
CMD ["gunicorn", "--config", "deploy/gunicorn.conf.py", "app:create_app()"]


FROM nginxinc/nginx-unprivileged:stable-alpine AS nginx
ARG REVISION=unknown
LABEL org.opencontainers.image.title="SellTrack Nginx" \
      org.opencontainers.image.source="https://github.com/wrouigb/SellTrack" \
      org.opencontainers.image.revision="$REVISION"
# Security fixes from since the base image was built.
USER root
RUN apk upgrade --no-cache
USER 101
COPY deploy/nginx.conf /etc/nginx/conf.d/default.conf
COPY deploy/down.html /usr/share/nginx/html/down.html
COPY --from=assets /build/app/static/dist /usr/share/nginx/html/static/dist
HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=3 \
    CMD ["wget", "-q", "-O", "/dev/null", "http://127.0.0.1:8080/nginx-ok"]
