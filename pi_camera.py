#!/usr/bin/env python3
"""
pi_camera.py — Bird Feeder Camera Client
=========================================
Runs on the Pi Zero. Captures JPEG frames and POSTs them to the server.

All traffic is outbound — no open ports needed on school/restricted networks.

Setup:
    sudo apt-get install -y python3-picamera2 python3-requests
    python3 pi_camera.py

Auto-start on boot:
    sudo nano /etc/systemd/system/feeder.service
    # Paste the unit file printed at startup, then:
    sudo systemctl enable feeder && sudo systemctl start feeder

CONFIGURATION — edit the constants below, no environment variables needed.
"""

import io
import sys
import time
import logging
import socket
import traceback

import requests
from requests.adapters import HTTPAdapter, Retry

# ══════════════════════════════════════════════════════════════════════════════
#  CONFIGURATION — change these to match your setup
# ══════════════════════════════════════════════════════════════════════════════

# Full URL of the push endpoint on your server.
PUSH_URL = "https://api.vatsaldutt.com/feed/push"

# Shared secret — must match FEEDER_PUSH_SECRET in server.py.
# Leave as empty string "" if server.py also has it as "".
PUSH_SECRET = ""

# Target frames per second. 6–8 is a good balance on Pi Zero + home WiFi.
FPS = 8

# JPEG quality (1–100). Lower = smaller payload, faster push.
JPEG_QUALITY = 70

# Output resolution delivered to this script (and sent to the server).
# This is the size of the JPEG frames the browser will see.
CAPTURE_WIDTH = 1280
CAPTURE_HEIGHT = 720

# RAW sensor resolution fed into the ISP pipeline.
# THIS IS THE KEY FIX FOR THE V4L2 DEQUEUE TIMEOUT:
#   The OV5647 defaults to 1920x1080 raw which is too slow for the Pi Zero's
#   1-second V4L2 dequeue timer — the sensor times out before delivering frame 1.
#   Forcing 640x480 raw runs at ~58 fps natively, comfortably beating the timer.
#   The ISP then upscales to CAPTURE_WIDTH x CAPTURE_HEIGHT for the output.
RAW_WIDTH = 640
RAW_HEIGHT = 480

# Seconds to wait after cam.start() before first capture.
# 3s gives the OV5647 time to lock auto-exposure at the lower raw resolution.
CAMERA_WARMUP_S = 3.0

# Frame duration range in microseconds.
# 640x480 @ 58fps native → min ~17ms. We target 8fps so max is generous.
# Never lock min==max — that forces the ISP onto a rigid schedule the sensor can't meet.
FRAME_DURATION_MIN_US = 33_000   # ~30 fps ceiling (sensor won't go faster than this)
FRAME_DURATION_MAX_US = 200_000  # ~5 fps floor

# How long to wait between retries when the camera or server has an error.
RETRY_PAUSE_S = 3.0

# ══════════════════════════════════════════════════════════════════════════════

INTERVAL = 1.0 / FPS  # seconds between frames

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)-5s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("feeder")


def _log_section(title: str):
    log.info("─" * 50)
    log.info(f"  {title}")
    log.info("─" * 50)


# ── Startup diagnostics ───────────────────────────────────────────────────────

def _check_network():
    """Confirm the Pi can reach the push server before doing anything else."""
    _log_section("NETWORK CHECK")
    host = PUSH_URL.split("/")[2]  # e.g. "api.vatsaldutt.com"
    try:
        ip = socket.gethostbyname(host)
        log.info(f"[OK]  DNS resolved {host!r} → {ip}")
    except socket.gaierror as e:
        log.error(f"[PI ERROR] DNS lookup failed for {host!r}: {e}")
        log.error("       Fix: check WiFi connection on the Pi.")
        log.error("       Run: ping 8.8.8.8  — if that fails, WiFi is down.")
        return False

    try:
        s = socket.create_connection((host, 443), timeout=5)
        s.close()
        log.info(f"[OK]  TCP connection to {host}:443 succeeded")
    except OSError as e:
        log.error(f"[PI ERROR] Cannot connect to {host}:443 — {e}")
        log.error("       Fix: check firewall/proxy rules on your network.")
        return False

    return True


def _check_server():
    """Hit /feed/debug to confirm the server has the bird feeder routes."""
    _log_section("SERVER CHECK")
    base = PUSH_URL.rsplit("/feed/", 1)[0]
    debug_url = f"{base}/feed/debug"
    status_url = f"{base}/feed/status"

    for url, label in [(status_url, "/feed/status"), (debug_url, "/feed/debug")]:
        try:
            r = requests.get(url, timeout=8)
            if r.status_code == 200:
                log.info(f"[OK]  {label} → HTTP 200")
            elif r.status_code == 404:
                log.error(f"[SERVER ERROR] {label} → 404 Not Found")
                log.error("       Fix: the server is running old code. Restart uvicorn.")
                log.error("       Run on server: sudo systemctl restart your-service")
                log.error(f"       Then verify: curl {url}")
                return False
            else:
                log.warning(f"[WARN] {label} → HTTP {r.status_code} (unexpected but not fatal)")
        except requests.exceptions.ConnectionError as e:
            log.error(f"[SERVER ERROR] Could not connect to {url}: {e}")
            log.error("       Fix: is the server process running? Check with: ps aux | grep uvicorn")
            return False
        except requests.exceptions.Timeout:
            log.error(f"[SERVER ERROR] {url} timed out")
            log.error("       Fix: server may be overloaded or Cloudflare Tunnel is down.")
            return False

    log.info("[OK]  Server has /feed/* routes registered and responding")
    return True


def _print_systemd_unit():
    """Print a ready-to-use systemd unit file for auto-start on boot."""
    script_path = __file__
    print()
    print("=" * 60)
    print("  SYSTEMD UNIT — paste into /etc/systemd/system/feeder.service")
    print("=" * 60)
    print(f"""
[Unit]
Description=Bird Feeder Camera
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 {script_path}
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
""")
    print("=" * 60)
    print()


# ── HTTP session ──────────────────────────────────────────────────────────────

def _make_session() -> requests.Session:
    """Create an HTTP session with retry logic for transient network failures."""
    _retry = Retry(
        total=5,
        backoff_factor=1.5,           # 1.5s, 3s, 6s, 12s, 24s between retries
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=["POST"],
        raise_on_status=False,
    )
    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=_retry))
    session.mount("http://", HTTPAdapter(max_retries=_retry))
    session.headers.update({"Content-Type": "image/jpeg"})
    if PUSH_SECRET:
        session.headers.update({"Authorization": f"Bearer {PUSH_SECRET}"})
    return session


_session = _make_session()


def _push_frame(jpeg_bytes: bytes) -> bool:
    """POST a single JPEG frame to the server. Returns True on success."""
    try:
        r = _session.post(
            PUSH_URL,
            data=jpeg_bytes,
            timeout=(5, 8),   # (connect timeout s, read timeout s)
        )
        if r.status_code == 204:
            return True

        # Surface the server's error message clearly
        body = r.text[:300].strip()
        if r.status_code == 401:
            log.error(f"[SERVER ERROR] Push rejected 401 Unauthorized: {body}")
            log.error("       Fix: PUSH_SECRET in pi_camera.py doesn't match server.py.")
        elif r.status_code == 403:
            log.error(f"[SERVER ERROR] Push rejected 403 Forbidden: {body}")
            log.error("       Fix: wrong PUSH_SECRET. Update pi_camera.py to match server.py.")
        elif r.status_code == 415:
            log.error(f"[PI ERROR] Push rejected 415: {body}")
            log.error("       Fix: camera is not sending valid JPEG bytes.")
        elif r.status_code == 404:
            log.error(f"[SERVER ERROR] Push returned 404: {body}")
            log.error("       Fix: server doesn't have /feed/push registered.")
            log.error("       Restart the server process with the updated server.py.")
        else:
            log.warning(f"[WARN] Push returned HTTP {r.status_code}: {body}")
        return False

    except requests.exceptions.ConnectionError as e:
        log.warning(f"[PI/SERVER] Connection error (WiFi drop or server down?): {e}")
        return False
    except requests.exceptions.Timeout:
        log.warning("[PI/SERVER] Push timed out — server slow or WiFi congested")
        return False
    except Exception as e:
        log.error(f"[PI ERROR] Unexpected push error: {e}")
        return False


# ── Camera loops ──────────────────────────────────────────────────────────────

def _capture_loop_picamera2():
    """Main capture loop using picamera2 (Pi Camera Module v2/v3)."""
    _log_section("CAMERA INIT (picamera2)")
    from picamera2 import Picamera2

    try:
        cam = Picamera2()
    except Exception as e:
        log.error(f"[PI ERROR] Could not open camera: {e}")
        log.error("       Fix: run 'vcgencmd get_camera' — if 'detected=0', the ribbon cable is loose.")
        raise

    try:
        config = cam.create_video_configuration(
            main={"size": (CAPTURE_WIDTH, CAPTURE_HEIGHT), "format": "RGB888"},
            # KEY FIX: force the 640x480 native sensor mode as the raw ISP input.
            # Without this, libcamera picks 1920x1080 raw, which clocks out too slowly
            # for the Pi Zero's 1-second V4L2 dequeue timer → camera times out on frame 1.
            # 640x480 runs at ~58fps natively, which easily beats the timer.
            # The ISP upscales to CAPTURE_WIDTH x CAPTURE_HEIGHT for the output stream.
            raw={"size": (RAW_WIDTH, RAW_HEIGHT)},
            controls={
                "FrameDurationLimits": (FRAME_DURATION_MIN_US, FRAME_DURATION_MAX_US),
            },
        )
        cam.configure(config)
        log.info(f"[OK]  Camera configured: output {CAPTURE_WIDTH}x{CAPTURE_HEIGHT} "
                 f"← raw {RAW_WIDTH}x{RAW_HEIGHT} sensor mode | "
                 f"frame window {FRAME_DURATION_MIN_US//1000}–{FRAME_DURATION_MAX_US//1000} ms")
    except Exception as e:
        log.error(f"[PI ERROR] Camera configuration failed: {e}")
        log.error("       If error mentions 'raw' size, your sensor doesn't support that mode.")
        log.error(f"       Try RAW_WIDTH=1296, RAW_HEIGHT=972 (other OV5647 native mode).")
        cam.close()
        raise

    try:
        cam.start()
        log.info(f"[OK]  Camera started — waiting {CAMERA_WARMUP_S}s for auto-exposure to settle")
        time.sleep(CAMERA_WARMUP_S)
        # Verify the camera is actually delivering frames by grabbing one test array.
        # This will raise immediately if the sensor is still timing out, rather than
        # failing silently and sending zero frames to the server.
        log.info("[OK]  Warmup done — verifying first frame delivery...")
        test = cam.capture_array("main")
        log.info(f"[OK]  Test frame OK: shape={test.shape}, dtype={test.dtype}")
        del test
    except Exception as e:
        log.error(f"[PI ERROR] Camera failed to start or deliver frames: {e}")
        log.error("       This is almost always a hardware issue:")
        log.error("       1. Reseat the ribbon cable on BOTH ends (Pi and camera).")
        log.error("       2. Check the camera connector lock is fully closed.")
        log.error("       3. Try: vcgencmd get_camera  →  should say supported=1 detected=1")
        cam.close()
        raise
        log.error("       This is usually a hardware issue — check ribbon cable seating.")
        cam.close()
        raise

    log.info(f"[OK]  Camera ready — pushing to {PUSH_URL}")
    log.info(f"       Target: {FPS} fps, JPEG quality {JPEG_QUALITY}, ~{INTERVAL*1000:.0f} ms/frame")

    ok_count = fail_count = consec_fail = 0
    last_report = time.monotonic()

    try:
        while True:
            t0 = time.monotonic()

            # ── Capture ───────────────────────────────────────────────────────
            try:
                buf = io.BytesIO()
                cam.capture_file(buf, format="jpeg")
                jpeg = buf.getvalue()
            except Exception as e:
                log.error(f"[PI ERROR] Frame capture failed: {e}")
                log.error("       Possible causes: camera unplugged, ribbon cable loose, libcamera crash.")
                consec_fail += 1
                if consec_fail >= 10:
                    log.error("[PI ERROR] 10 consecutive capture failures — restarting camera")
                    raise  # bubble up to outer restart loop
                time.sleep(RETRY_PAUSE_S)
                continue

            # ── Validate JPEG ─────────────────────────────────────────────────
            if len(jpeg) < 100 or jpeg[:2] != b"\xff\xd8":
                log.error(f"[PI ERROR] Captured data is not a valid JPEG ({len(jpeg)} bytes). "
                          "Camera driver issue — try rebooting the Pi.")
                consec_fail += 1
                time.sleep(RETRY_PAUSE_S)
                continue

            # ── Push ──────────────────────────────────────────────────────────
            ok = _push_frame(jpeg)
            if ok:
                ok_count += 1
                consec_fail = 0
            else:
                fail_count += 1
                consec_fail += 1

            # ── Periodic stats report ─────────────────────────────────────────
            now = time.monotonic()
            if now - last_report >= 30:
                total = ok_count + fail_count
                rate = ok_count / max(total, 1) * 100
                kb = len(jpeg) / 1024
                log.info(
                    f"[STATS] {ok_count} ok / {fail_count} failed ({rate:.0f}% success) | "
                    f"last frame {kb:.1f} KB"
                )
                last_report = now

            # ── Pace to target FPS ────────────────────────────────────────────
            elapsed = time.monotonic() - t0
            sleep_for = max(0.0, INTERVAL - elapsed)
            time.sleep(sleep_for)

    except KeyboardInterrupt:
        log.info("Interrupted by user — shutting down")
    finally:
        try:
            cam.stop()
            log.info("Camera stopped cleanly")
        except Exception as e:
            log.warning(f"[PI WARN] cam.stop() raised an exception (safe to ignore): {e}")


def _capture_loop_opencv():
    """Fallback capture loop using OpenCV + V4L2 (USB webcams or legacy Pi cameras)."""
    _log_section("CAMERA INIT (OpenCV/V4L2 fallback)")
    import cv2

    log.info(f"Trying /dev/video0 at {CAPTURE_WIDTH}x{CAPTURE_HEIGHT}, target {FPS} fps")
    cap = cv2.VideoCapture(0)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, CAPTURE_WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAPTURE_HEIGHT)
    cap.set(cv2.CAP_PROP_FPS, FPS)

    if not cap.isOpened():
        log.error("[PI ERROR] Could not open /dev/video0 — is a camera connected?")
        log.error("       Run: ls /dev/video* — if nothing listed, no camera detected.")
        sys.exit(1)

    log.info(f"[OK]  Camera ready via OpenCV — pushing to {PUSH_URL}")
    encode_params = [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY]
    ok_count = fail_count = 0

    try:
        while True:
            t0 = time.monotonic()
            ret, frame = cap.read()
            if not ret:
                log.warning("[PI WARN] Frame capture failed — retrying")
                time.sleep(0.5)
                continue

            ok, buf = cv2.imencode(".jpg", frame, encode_params)
            if not ok:
                log.warning("[PI WARN] JPEG encode failed")
                continue

            if _push_frame(buf.tobytes()):
                ok_count += 1
            else:
                fail_count += 1

            if (ok_count + fail_count) % 100 == 0:
                log.info(f"[STATS] {ok_count} ok / {fail_count} failed")

            elapsed = time.monotonic() - t0
            time.sleep(max(0.0, INTERVAL - elapsed))

    except KeyboardInterrupt:
        log.info("Interrupted by user — shutting down")
    finally:
        cap.release()
        log.info("Camera released")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    _log_section("BIRD FEEDER CAMERA STARTING")
    log.info(f"Push URL:   {PUSH_URL}")
    log.info(f"Auth:       {'enabled (' + str(len(PUSH_SECRET)) + ' char secret)' if PUSH_SECRET else 'disabled (no secret)'}")
    log.info(f"Resolution: {CAPTURE_WIDTH}x{CAPTURE_HEIGHT}")
    log.info(f"Target FPS: {FPS}  (frame window: {FRAME_DURATION_MIN_US//1000}–{FRAME_DURATION_MAX_US//1000} ms)")
    log.info(f"JPEG quality: {JPEG_QUALITY}")
    _print_systemd_unit()

    # ── Pre-flight checks ──────────────────────────────────────────────────────
    if not _check_network():
        log.error("[FATAL] Network check failed — fix WiFi before proceeding.")
        sys.exit(1)

    if not _check_server():
        log.error("[FATAL] Server check failed — fix server before proceeding.")
        log.error("       If you just deployed the server, wait 10s and try again.")
        sys.exit(1)

    _log_section("ALL CHECKS PASSED — STARTING CAPTURE")

    # ── Camera selection and outer restart loop ───────────────────────────────
    use_picamera2 = False
    use_opencv = True
    log.info("[FORCED] Using OpenCV (V4L2) with camera index 0")

    if not use_picamera2:
        try:
            import cv2  # noqa: F401
            use_opencv = True
            log.info("[OK]  OpenCV found — using V4L2 driver")
        except ImportError:
            log.error("[PI ERROR] Neither picamera2 nor OpenCV (cv2) is installed.")
            log.error("  Pi Camera Module: sudo apt-get install python3-picamera2")
            log.error("  USB webcam:       sudo apt-get install python3-opencv")
            sys.exit(1)

    # Outer loop: restart camera if it crashes, rather than killing the whole process.
    restart_count = 0
    while True:
        try:
            if use_picamera2:
                _capture_loop_picamera2()
            else:
                _capture_loop_opencv()
            # If loop returns cleanly (KeyboardInterrupt handled inside), exit.
            break
        except KeyboardInterrupt:
            log.info("Shutting down")
            break
        except Exception as e:
            restart_count += 1
            log.error(f"[PI ERROR] Camera loop crashed (restart #{restart_count}): {e}")
            log.error(traceback.format_exc())
            if restart_count >= 5:
                log.error("[FATAL] Camera has crashed 5 times. Giving up.")
                log.error("       Check hardware: ribbon cable, power supply, camera module.")
                sys.exit(1)
            log.info(f"Restarting camera in {RETRY_PAUSE_S}s...")
            time.sleep(RETRY_PAUSE_S)
