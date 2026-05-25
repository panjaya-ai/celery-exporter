import sys
import tracemalloc
from threading import Thread

import kombu.exceptions
from flask import Blueprint, Flask, current_app, request
from loguru import logger
from prometheus_client.exposition import choose_encoder
from waitress import serve

blueprint = Blueprint("celery_exporter", __name__)


@blueprint.route("/")
def index():
    return """
<!doctype html>
<html lang="en">
  <head>
    <!-- Required meta tags -->
    <meta charset="utf-8">
    <title>celery-exporter</title>
  </head>
  <body>
    <h1>Celery Exporter</h1>
    <p><a href="/metrics">Metrics</a></p>
  </body>
</html>
"""


@blueprint.route("/metrics")
def metrics():
    encoder, content_type = choose_encoder(request.headers.get("accept"))
    # Serialise with the background bulk-write phase in
    # Exporter.track_queue_metrics so /metrics sees an atomic snapshot
    # of the gauges rather than a partial mid-write state.
    with current_app.config["metrics_lock"]:
        output = encoder(current_app.config["registry"])
    return output, 200, {"Content-Type": content_type}


@blueprint.route("/health")
def health():
    conn = current_app.config["celery_connection"]
    uri = conn.as_uri()

    try:
        conn.ensure_connection(max_retries=3)
    except kombu.exceptions.OperationalError:
        logger.error("Failed to connect to broker='{}'", uri)
        return (f"Failed to connect to broker: '{uri}'", 500)
    except Exception:  # pylint: disable=broad-except
        logger.exception("Unrecognized error")
        return ("Unknown exception", 500)
    return f"Connected to the broker {conn.as_uri()}"


@blueprint.route("/debug/tracemalloc")
def debug_tracemalloc():
    # Return the top-N Python allocation sites by total bytes. Hit this
    # periodically while reproducing the leak; growth in a specific file:line
    # is the smoking gun. Output is plain text formatted exactly like
    # `tracemalloc.Snapshot.statistics()`.
    if not tracemalloc.is_tracing():
        return ("tracemalloc not started", 503, {"Content-Type": "text/plain"})
    n = int(request.args.get("n", "30"))
    snapshot = tracemalloc.take_snapshot()
    top_by_lineno = snapshot.statistics("lineno")[:n]
    by_file = snapshot.statistics("filename")
    total_bytes = sum(stat.size for stat in by_file)
    current, peak = tracemalloc.get_traced_memory()
    lines = [
        f"tracemalloc current: {current / 1024 / 1024:.2f} MiB",
        f"tracemalloc peak:    {peak / 1024 / 1024:.2f} MiB",
        f"sum of all stats:    {total_bytes / 1024 / 1024:.2f} MiB",
        "",
        f"Top {n} allocation sites by total bytes (group_by=lineno):",
    ]
    for i, stat in enumerate(top_by_lineno, 1):
        lines.append(f"#{i}: {stat}")
        # Include the first frame of the traceback for context.
        if stat.traceback:
            for frame in list(stat.traceback)[:3]:
                lines.append(f"    {frame.filename}:{frame.lineno}")
    lines.append("")
    lines.append("Top 15 files by total bytes:")
    for stat in by_file[:15]:
        lines.append(f"  {stat.size / 1024:.1f} KiB  count={stat.count}  {stat.traceback[0].filename}")
    return "\n".join(lines), 200, {"Content-Type": "text/plain"}


@blueprint.route("/debug/mailbox")
def debug_mailbox():
    # Inspect self.app.control.mailbox.unclaimed — the kombu pidbox
    # accumulator at kombu/pidbox.py:191. Stash for orphan reply tickets;
    # has no cleanup path. If this dict grows monotonically across calls
    # to this endpoint, it's a contributor to the exporter's RSS leak.
    exporter = current_app.config.get("exporter")
    if exporter is None or not hasattr(exporter, "app"):
        return ("exporter not available", 503, {"Content-Type": "text/plain"})
    try:
        mailbox = exporter.app.control.mailbox
    except Exception as e:  # pylint: disable=broad-except
        return (f"mailbox not available: {e!r}", 503, {"Content-Type": "text/plain"})
    unclaimed = getattr(mailbox, "unclaimed", None)
    if unclaimed is None:
        return ("mailbox has no unclaimed attribute", 503, {"Content-Type": "text/plain"})
    # Snapshot a shallow copy of the keys so we don't iterate a dict that
    # the metrics-loop thread might be mutating. dict.copy() is atomic.
    items = list(unclaimed.items())
    total_entries = sum(len(deque) for _, deque in items)
    approx_bytes = sys.getsizeof(unclaimed)
    for k, v in items:
        approx_bytes += sys.getsizeof(k) + sys.getsizeof(v)
        for item in v:
            approx_bytes += sys.getsizeof(item)
    top = sorted(((str(k), len(v)) for k, v in items), key=lambda x: -x[1])[:20]
    lines = [
        f"unclaimed tickets: {len(items)}",
        f"total entries across all deques: {total_entries}",
        f"approx total bytes (shallow getsizeof): {approx_bytes}",
        "",
        "Top 20 tickets by entry count:",
    ]
    for ticket, count in top:
        lines.append(f"  {ticket}: {count}")
    return "\n".join(lines), 200, {"Content-Type": "text/plain"}


def _serve_with_error_logging(app, host, port):
    # The waitress thread was previously daemon=True with `_quiet=True`
    # and no exception handling, so any startup failure (port bind error,
    # import error, missing kombu deps in the slim image) was silently
    # swallowed — the main thread logged "Started celery-exporter..."
    # while the bind never happened. This wrapper surfaces the actual
    # exception so we can see why startup fails in the container.
    try:
        logger.info("waitress: about to bind host={} port={}", host, port)
        serve(app, host=host, port=port, _quiet=False)
    except SystemExit:
        raise
    except BaseException:  # pylint: disable=broad-except
        logger.exception("waitress crashed during serve()")
        raise


def start_http_server(registry, celery_connection, host, port, metrics_lock, exporter=None):
    app = Flask(__name__)
    app.config["registry"] = registry
    app.config["celery_connection"] = celery_connection
    app.config["metrics_lock"] = metrics_lock
    app.config["exporter"] = exporter
    app.register_blueprint(blueprint)
    Thread(
        target=_serve_with_error_logging,
        args=(app, host, port),
        daemon=True,
    ).start()
    logger.info("Started celery-exporter at host='{}' on port='{}'", host, port)
