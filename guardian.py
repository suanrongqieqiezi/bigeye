#!/usr/bin/env python3
"""
Guardian for 大眼X — process supervision and version management.

Architecture:
  server.py
       ↓
  ┌─────────────────────────────────────┐
  │  guardian.py                        │
  │  ├── Active       → 9890 (user)     │
  │  ├── Guardian API → 9891 (control)  │
  │  └── /api/rollback → version restore│
  └─────────────────────────────────────┘

Single source of truth at project root: server.py, db.py, public/, etc.
Runtime data: chat.db, memory/, missions/.
"""
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import http.server
import urllib.request
import traceback
from state_manager import (
    load_guardian_state, save_guardian_state,
    load_server_state, save_server_state,
    log_progress, read_progress,
)
import version_manager

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
try:
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

FROZEN = getattr(sys, 'frozen', False)
BASE_DIR = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
# Writable data goes alongside the exe in frozen mode, in source dir otherwise
ROOT_DIR = os.path.dirname(sys.executable) if FROZEN else os.path.dirname(os.path.abspath(__file__))
MAIN_SCRIPT = os.path.join(BASE_DIR, "rpc_server.py")

# ── Shared log file (for GUI panel) ────────────────
LOG_PATH = os.path.join(ROOT_DIR, "_server.log")
_log_file = None

def _log_write(text):
    """Write to both stdout and shared log file."""
    global _log_file
    try:
        print(text)
        if _log_file is not None:
            _log_file.write(text + "\n")
            _log_file.flush()
    except Exception:
        pass

MAIN_PORT = 8765       # user-facing
GUARDIAN_PORT = 9891    # guardian control plane
HEALTH_POLL = 5         # seconds between health checks
HEALTH_TIMEOUT = 20     # max wait for slot to become healthy
MAX_RESTARTS = 5
COOLDOWN = 60

def _read_port_setting(key, default):
    """Read a port setting from chat.db if it exists."""
    try:
        db_path = os.path.join(ROOT_DIR, "data", "chat.db")
        if os.path.exists(db_path):
            import sqlite3 as _sql
            c = _sql.connect(db_path)
            row = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            c.close()
            if row and row[0].isdigit():
                return int(row[0])
    except:
        pass
    return default


def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def check_health(port, timeout=3):
    """Hit /api/health on a given port."""
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/health")
        resp = urllib.request.urlopen(req, timeout=timeout)
        data = json.loads(resp.read())
        return data.get("status") == "ok", data
    except Exception as e:
        return False, {"error": str(e)}


    # ── Guardian Health Handler (port 9891) ──────────

class GuardianHandler(http.server.BaseHTTPRequestHandler):
    guardian = None

    def log_message(self, format, *args):
        pass

    def _json(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _serve_html(self, path, code=200):
        full = os.path.join(BASE_DIR, path)
        if not os.path.exists(full):
            self._json(404, {"error": "not found"})
            return
        with open(full, "r", encoding="utf-8") as f:
            body = f.read().encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        try:
            self._do_GET()
        except Exception as e:
            _log_write(f"[guardian] GET {self.path} ERROR: {e}")
            try: self._json(500, {"error": str(e)})
            except: pass

    def _do_GET(self):
        g = self.guardian
        if self.path == "/emergency" or (g._emergency and self.path == "/"):
            self._serve_html("emergency.html")
        elif self.path == "/api/health":
            active_ok, active_data = check_health(MAIN_PORT)
            self._json(200, {
                "status": g.state.get("status", "unknown"),
                "pid": os.getpid(),
                "active_slot": g.active_slot,
                "active_healthy": active_ok,
                "active_data": active_data,
                "restart_count": g.state.get("restart_count", 0),
                "upgrade_count": g.state.get("upgrade_count", 0),
                "emergency": g._emergency,
            })
        elif self.path == "/api/status":
            active_ok, _ = check_health(MAIN_PORT)
            self._json(200, {
                "status": g.state.get("status", "unknown"),
                "active_slot": g.active_slot,
                "active_healthy": active_ok,
                "upgrade_count": g.state.get("upgrade_count", 0),
                "emergency": g._emergency,
            })
        elif self.path == "/api/progress":
            entries = read_progress(limit=30)
            self._json(200, {"entries": entries})
        elif self.path == "/api/state":
            vi = version_manager.get_current_info()
            self._json(200, {
                "guardian": g.state,
                "version": vi,
            })
        elif self.path == "/api/versions":
            archives = version_manager.list_archives()
            cur = version_manager.compute_state_hash()
            self._json(200, {"archives": archives, "current_hash": cur[:12] if cur else None})
        elif self.path == "/api/version-info":
            self._json(200, version_manager.get_current_info())
        elif self.path == "/api/ompq-status" or self.path == "/api/server-status":
            ok, data = check_health(MAIN_PORT)
            self._json(200, {"exists": True, "running": ok})
        else:
            self._json(404, {})

    def do_POST(self):
        try:
            self._do_POST()
        except Exception as e:
            _log_write(f"[guardian] POST {self.path} ERROR: {e}")
            try: self._json(500, {"error": str(e)})
            except: pass

    def _do_POST(self):
        g = self.guardian
        data = {}
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length > 0:
                data = json.loads(self.rfile.read(length))
        except:
            pass

        if self.path == "/api/rollback":
            version = data.get("version", "")
            if not version:
                self._json(400, {"error": "version required"})
                return
            def do_rollback():
                version_manager.archive(label="pre_rollback")
                ok, files = version_manager.rollback(version)
                if ok:
                    _log_write(f"[guardian] Rollback to {version} — {len(files)} files restored")
                    log_progress("version_rollback_done", version=version, files=files)
                    if g._emergency:
                        g.exit_emergency()
                    else:
                        g.restart_active()
            threading.Thread(target=do_rollback, daemon=True).start()
            self._json(202, {"success": True, "status": "rollback_started"})
        elif self.path == "/api/restart":
            if g._emergency:
                threading.Thread(target=g.exit_emergency, daemon=True).start()
                self._json(202, {"status": "exiting_emergency"})
            else:
                threading.Thread(target=g.restart_active, daemon=True).start()
                self._json(202, {"status": "restart_started"})
        elif self.path == "/api/shutdown":
            self._json(200, {"status": "shutting_down"})
            threading.Thread(target=g.shutdown, daemon=True).start()
        elif self.path == "/api/emergency":
            g.enter_emergency("manual")
            self._json(200, {"status": "emergency_mode"})
        elif self.path == "/api/exit-emergency":
            threading.Thread(target=g.exit_emergency, daemon=True).start()
            self._json(202, {"status": "exiting_emergency"})
        else:
            self._json(404, {})


# ── Guardian ─────────────────────────────────────

class Guardian:
    def __init__(self):
        self.state = load_guardian_state()
        self.state.setdefault("active_slot", "A")
        self.state.setdefault("restart_count", 0)
        self.state.setdefault("upgrade_count", 0)
        self.state.setdefault("status", "init")
        self.active_proc = None
        self.verify_proc = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._last_restart = 0
        self._emergency = False

        # Auto-archive on startup if code changed
        try:
            if version_manager.has_changes():
                version_manager.archive(label="auto")
        except Exception as e:
            _log_write(f"[guardian] Version archive skipped: {e}")
        # Guardian self-integrity check — detect if guardian.py was tampered
        try:
            gpath = os.path.join(BASE_DIR, "guardian.py") if FROZEN else os.path.join(ROOT_DIR, "guardian.py")
            cur_g = version_manager._hash_file(gpath)
            manifest = version_manager._load_manifest()
            last_g = manifest.get("guardian_hash")
            if last_g and cur_g and cur_g != last_g:
                _log_write(f"[guardian] ⚠ 守护代码已变更！上次={last_g[:12]} 当前={cur_g[:12]}")
                log_progress("guardian_code_changed", last=last_g[:12], current=cur_g[:12])
            if cur_g:
                manifest["guardian_hash"] = cur_g
                version_manager._save_manifest(manifest)
        except Exception as e:
            _log_write(f"[guardian] Self-check skipped: {e}")
    @property
    def active_slot(self):
        return self.state.get("active_slot", "main")

    @property
    def active_script(self):
        return MAIN_SCRIPT

    @property
    def active_dir(self):
        return ROOT_DIR

    def _script_label(self, which="active"):
        return "main"

    # ── Process management ───────────────────────

    def _start_slot(self, script, port, label, timeout=HEALTH_TIMEOUT):
        """Start a slot process and wait for health."""
        cwd = os.path.dirname(script) if not FROZEN else ROOT_DIR
        _log_write(f"[guardian] Starting {label} on port {port}: {script}")
        if FROZEN:
            cmd = [sys.executable, "-u", "--rpc-server", "--port", str(port), "--label", label]
        else:
            cmd = [sys.executable, "-u", script, "--port", str(port), "--label", label]
        popen_kwargs = dict(
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1, cwd=cwd,
        )
        if sys.platform == 'win32':
            popen_kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
        proc = subprocess.Popen(cmd, **popen_kwargs)
        threading.Thread(target=self._read_output, args=(proc, label), daemon=True).start()

        deadline = time.time() + timeout
        while time.time() < deadline:
            if proc.poll() is not None:
                return None  # crashed
            ok, _ = check_health(port)
            if ok:
                _log_write(f"[guardian] {label} healthy (pid={proc.pid})")
                return proc
            time.sleep(1)
        return None

    def _stop_slot(self, proc, label, timeout=10):
        """Gracefully stop a slot process."""
        if proc is None or proc.poll() is not None:
            return
        _log_write(f"[guardian] Stopping {label} (pid={proc.pid})")
        proc.terminate()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3)

    def _read_output(self, proc, label):
        try:
            for line in proc.stdout:
                line = line.rstrip()
                if line:
                    _log_write(f"[{label}] {line}")
        except Exception:
            pass

    # ── Lifecycle ─────────────────────────────────

    def start_active(self):
        """Start the active slot on MAIN_PORT."""
        with self._lock:
            if self.active_proc is not None and self.active_proc.poll() is None:
                return True  # already running

            self.state["status"] = "starting"
            save_guardian_state(self.state)

            label = self._script_label("active")
            proc = self._start_slot(self.active_script, MAIN_PORT, label)
            if proc is None:
                self.state["status"] = "failed"
                save_guardian_state(self.state)
                log_progress("start_failed", slot=self.active_slot)
                return False

            self.active_proc = proc
            self.state["status"] = "running"
            self.state["active_pid"] = proc.pid
            save_guardian_state(self.state)
            log_progress("active_started", slot=self.active_slot, pid=proc.pid)
            return True

    def _cleanup_orphans(self):
        """No orphan cleanup needed — 大眼X uses direct LLM calls, no external process."""
        pass
        self._last_restart = now

        log_progress("restart_begin", slot=self.active_slot)
        self._stop_slot(self.active_proc, self._script_label("active"))
        self.active_proc = None
        time.sleep(1)
        self._cleanup_orphans()
        time.sleep(1)
        ok = self.start_active()
        if ok:
            self.state["restart_count"] += 1
            save_guardian_state(self.state)
            log_progress("restart_done", slot=self.active_slot)
        else:
            log_progress("restart_failed", slot=self.active_slot)
        return ok



    def enter_emergency(self, reason="max_restarts"):
        """Enter emergency recovery mode — serve recovery UI instead of main app."""
        if self._emergency:
            return
        self._emergency = True
        self.state["status"] = "emergency"
        save_guardian_state(self.state)
        log_progress("emergency_enter", reason=reason)
        _log_write(f"[guardian] 🛟 EMERGENCY MODE — {reason}")
        _log_write("[guardian]    Recovery page at http://127.0.0.1:9891/emergency")

    def exit_emergency(self):
        """Exit emergency mode and try to start active slot."""
        self._emergency = False
        self.state["restart_count"] = 0
        self.state["status"] = "starting"
        save_guardian_state(self.state)
        log_progress("emergency_exit")
        _log_write("[guardian] Exiting emergency mode — attempting normal start")
        self.start_active()


    def shutdown(self):
        _log_write("[guardian] Shutting down...")
        self._stop.set()
        self._stop_slot(self.active_proc, self._script_label("active"))
        self._stop_slot(self.verify_proc, "verify")
        self.state["status"] = "stopped"
        save_guardian_state(self.state)
        log_progress("guardian_stop")

    def monitor_loop(self):
        try:
            self._monitor_loop()
        except Exception as e:
            _log_write(f"[guardian] FATAL monitor error: {e}")
            import traceback
            traceback.print_exc()
            self.enter_emergency("monitor_crash")

    def _monitor_loop(self):
        _log_write(f"[guardian] Monitor started — active slot: {self.active_slot}")
        self.state["status"] = "running"
        save_guardian_state(self.state)

        while not self._stop.is_set():
            if self._emergency:
                self._stop.wait(HEALTH_POLL)
                continue

            proc = self.active_proc
            if proc is not None:
                rc = proc.poll()
                if rc is not None:
                    _log_write(f"[guardian] Active slot exited (code={rc})")
                    log_progress("active_exit", slot=self.active_slot, exit_code=rc)
                    self.active_proc = None

                    if self.state["status"] == "upgrading":
                        pass
                    elif self.state["restart_count"] < MAX_RESTARTS:
                        _log_write(f"[guardian] Auto-restart (attempt {self.state['restart_count']+1})")
                        self.restart_active()
                    else:
                        _log_write("[guardian] Max restarts — entering emergency mode")
                        self.enter_emergency("max_restarts")

            if self.active_proc is None and self.state["status"] == "running":
                if self.state["restart_count"] < MAX_RESTARTS:
                    _log_write(f"[guardian] No active process — restarting (attempt {self.state['restart_count']+1})")
                    if not self.restart_active():
                        time.sleep(HEALTH_POLL)
                else:
                    _log_write("[guardian] Max restarts — entering emergency mode")
                    self.enter_emergency("max_restarts")

            if self.active_proc is not None and self.active_proc.poll() is None:
                ok, _ = check_health(MAIN_PORT)
                if not ok and self.state["status"] == "running":
                    _log_write("[guardian] ⚠ Active unhealthy")
                elif ok:
                    if time.time() - self._last_restart > COOLDOWN:
                        self.state["restart_count"] = 0
                    save_guardian_state(self.state)

            self._stop.wait(HEALTH_POLL)


# ── Main ─────────────────────────────────────────

def main():
    try:
        import multiprocessing
        multiprocessing.freeze_support()
    except ImportError:
        pass

    # ── Hide console window (Windows) ──
    if sys.platform == 'win32' and not FROZEN:
        try:
            import ctypes as _ct
            _ct.windll.user32.ShowWindow(_ct.windll.kernel32.GetConsoleWindow(), 0)
        except Exception:
            pass

    # ── Open shared log file (truncate to last 2000 lines) ──
    global _log_file
    try:
        # Rotate: keep last 2000 lines
        if os.path.exists(LOG_PATH):
            with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            if len(lines) > 2000:
                with open(LOG_PATH, "w", encoding="utf-8") as f:
                    f.writelines(lines[-2000:])
        _log_file = open(LOG_PATH, "a", encoding="utf-8")
    except Exception:
        pass

    # ── Cleanup orphan ompQ from previous runs ──
    try:
        if sys.platform == 'win32':
            subprocess.run(["taskkill", "/f", "/im", "ompQ.exe"],
                           capture_output=True, timeout=5)
    except Exception:
        pass

    # Override ports from DB if user saved custom settings
    global MAIN_PORT, GUARDIAN_PORT
    MAIN_PORT = _read_port_setting("main_port", MAIN_PORT)
    GUARDIAN_PORT = _read_port_setting("guardian_port", GUARDIAN_PORT)
    g = Guardian()

    _log_write(f"[guardian] ═══════════════════════════════════════════")
    _log_write(f"[guardian]   大眼X Guardian — direct LLM, no OMP")
    _log_write(f"[guardian]   Active:  server.py")
    _log_write(f"[guardian]   Port:    {MAIN_PORT} (main), {GUARDIAN_PORT} (control)")
    _log_write(f"[guardian] ═══════════════════════════════════════════")

    # Start active
    if not g.start_active():
        _log_write("[guardian] Failed to start active slot — exiting")
        sys.exit(1)

    local_ip = get_local_ip()
    _log_write(f"[guardian] Active:       http://{local_ip}:{MAIN_PORT}")
    _log_write(f"[guardian] Guardian API: http://{local_ip}:{GUARDIAN_PORT}/api/health")

    # Start guardian HTTP handler
    GuardianHandler.guardian = g
    try:
        healthd = http.server.HTTPServer(("0.0.0.0", GUARDIAN_PORT), GuardianHandler)
    except OSError:
        _log_write(f"[guardian] Port {GUARDIAN_PORT} busy, waiting...")
        time.sleep(5)
        healthd = http.server.HTTPServer(("0.0.0.0", GUARDIAN_PORT), GuardianHandler)

    health_thread = threading.Thread(target=healthd.serve_forever, daemon=True)
    health_thread.start()

    # Monitor loop
    try:
        g.monitor_loop()
    except KeyboardInterrupt:
        _log_write("[guardian] Interrupted")
    finally:
        g.shutdown()
        healthd.shutdown()


if __name__ == "__main__":
    if "--rpc-server" in sys.argv:
        # Running as rpc_server subprocess — delegate to rpc_server.main()
        sys.argv.remove("--rpc-server")
        if "-u" in sys.argv:
            sys.argv.remove("-u")
        import rpc_server
        rpc_server.main()
    else:
        main()
