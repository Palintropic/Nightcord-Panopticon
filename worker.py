"""Run periodic monitoring tasks without starting a HTTP listener.

Use this alongside Gunicorn in production.  It deliberately keeps the alert
loop in the foreground, so systemd can restart it if the worker exits.
"""
import threading

from app import background_alert_loop, background_metrics_prune_loop


if __name__ == "__main__":
    threading.Thread(target=background_metrics_prune_loop, daemon=True).start()
    background_alert_loop()
