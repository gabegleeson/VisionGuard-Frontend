# api_client.py
# Handles sending alert payloads to the VisionGuard cloud dashboard via HTTPS.

import logging
import os
import threading
from datetime import datetime, timezone

import requests

# ---------------------------------------------------------------------------
# Configuration — set these in your environment or a .env file.
# Never hardcode secrets in source code.
# ---------------------------------------------------------------------------
API_BASE_URL = os.getenv("VISIONGUARD_API_URL", "")
API_KEY      = os.getenv("VISIONGUARD_API_KEY", "")

# How long (seconds) to wait for the server before giving up
REQUEST_TIMEOUT = 5

logger = logging.getLogger(__name__)


def _build_headers() -> dict:
    """Return the HTTP headers required by the dashboard API."""
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {API_KEY}",
    }


def send_alert(alert_type: str, detail: str, camera_source: str) -> bool:
    """
    POST a single alert to the dashboard API.

    Parameters
    ----------
    alert_type    : One of 'blur', 'darkness', 'color', 'tiles', 'ssim'
    detail        : Human-readable description, e.g. 'Score: 42.3'
    camera_source : Camera index or RTSP URL as a string

    Returns True on success, False on any error.
    This function is blocking — call send_alert_async() from the monitor loop.
    """
    payload = {
        "alert_type":    alert_type,
        "detail":        detail,
        "camera_source": camera_source,
        "timestamp":     datetime.now(timezone.utc).isoformat(),
    }

    try:
        response = requests.post(
            url=f"{API_BASE_URL}/alerts",
            json=payload,
            headers=_build_headers(),
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()  # Raises for 4xx / 5xx responses
        logger.info(f"[API] Alert sent — type={alert_type} status={response.status_code}")
        return True

    except requests.exceptions.ConnectionError:
        logger.warning("[API] Could not reach dashboard — no network connection.")
    except requests.exceptions.Timeout:
        logger.warning(f"[API] Request timed out after {REQUEST_TIMEOUT}s.")
    except requests.exceptions.HTTPError as e:
        logger.warning(f"[API] Server returned an error: {e}")
    except Exception as e:
        logger.warning(f"[API] Unexpected error sending alert: {e}")

    return False


def send_alert_async(alert_type: str, detail: str, camera_source: str) -> None:
    """
    Non-blocking version of send_alert().
    Fires the HTTP request on a background daemon thread so the
    monitoring loop is never stalled by network latency.
    """
    thread = threading.Thread(
        target=send_alert,
        args=(alert_type, detail, camera_source),
        daemon=True,  # Thread dies automatically when the main process exits
    )
    thread.start()


def send_heartbeat(camera_source: str) -> bool:
    """
    POST a lightweight 'camera alive' ping to the dashboard.
    Call this on a timer (e.g. every 30 s) so the dashboard can
    display live connectivity status for each camera.
    """
    payload = {
        "camera_source": camera_source,
        "timestamp":     datetime.now(timezone.utc).isoformat(),
        "status":        "online",
    }

    try:
        response = requests.post(
            url=f"{API_BASE_URL}/heartbeat",
            json=payload,
            headers=_build_headers(),
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        return True

    except Exception as e:
        logger.warning(f"[API] Heartbeat failed: {e}")
        return False