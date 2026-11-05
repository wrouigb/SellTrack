"""Gunicorn settings. Two workers with four threads each suits a Pi and a few hundred users."""

import os

bind = "0.0.0.0:8000"
worker_class = "gthread"
workers = int(os.environ.get("WEB_WORKERS", "2"))
threads = int(os.environ.get("WEB_THREADS", "4"))

# Load the app once, then fork, so that the workers share most of its memory. Nothing connects to
# the database or Redis until the first request, so no connection ends up shared between them.
preload_app = True

timeout = 60
graceful_timeout = 30
keepalive = 5

# Restarting a worker now and then puts a ceiling on any slow memory growth.
max_requests = 2000
max_requests_jitter = 200

# The file system is read-only, and the heartbeat file has to live somewhere fast.
worker_tmp_dir = "/dev/shm"  # noqa: S108 - in memory, and private to this container

# Gunicorn's runtime control socket isn't used, and it needs somewhere to write. Off means there's
# nothing extra to secure.
control_socket_disable = True

# The app logs every request itself, as JSON with a request ID.
accesslog = None
errorlog = "-"
