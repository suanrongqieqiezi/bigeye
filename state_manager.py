#!/usr/bin/env python3
"""
State manager for 大眼X Guardian.
Manages persistent state for guardian and server processes.
"""
import json
import os
import sys
import time

# State file locations
FROZEN = getattr(sys, 'frozen', False)
BASE_DIR = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
ROOT_DIR = os.path.dirname(sys.executable) if FROZEN else os.path.dirname(os.path.abspath(__file__))

GUARDIAN_STATE_FILE = os.path.join(ROOT_DIR, "data", "guardian_state.json")
SERVER_STATE_FILE = os.path.join(ROOT_DIR, "data", "server_state.json")
PROGRESS_LOG_FILE = os.path.join(ROOT_DIR, "data", "progress.jsonl")


def _ensure_data_dir():
    """Ensure data directory exists."""
    data_dir = os.path.join(ROOT_DIR, "data")
    if not os.path.exists(data_dir):
        os.makedirs(data_dir, exist_ok=True)


def load_guardian_state():
    """Load guardian state from JSON file."""
    _ensure_data_dir()
    if not os.path.exists(GUARDIAN_STATE_FILE):
        return {}
    try:
        with open(GUARDIAN_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_guardian_state(state):
    """Save guardian state to JSON file."""
    _ensure_data_dir()
    try:
        with open(GUARDIAN_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[state_manager] Failed to save guardian state: {e}")


def load_server_state():
    """Load server state from JSON file."""
    _ensure_data_dir()
    if not os.path.exists(SERVER_STATE_FILE):
        return {}
    try:
        with open(SERVER_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_server_state(state):
    """Save server state to JSON file."""
    _ensure_data_dir()
    try:
        with open(SERVER_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[state_manager] Failed to save server state: {e}")


def log_progress(event, **kwargs):
    """Log a progress event to JSONL file."""
    _ensure_data_dir()
    entry = {
        "timestamp": time.time(),
        "event": event,
    }
    entry.update(kwargs)
    try:
        with open(PROGRESS_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[state_manager] Failed to log progress: {e}")


def read_progress(limit=50):
    """Read recent progress entries from JSONL file."""
    _ensure_data_dir()
    if not os.path.exists(PROGRESS_LOG_FILE):
        return []
    try:
        entries = []
        with open(PROGRESS_LOG_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except Exception:
                        pass
        # Return most recent entries
        return entries[-limit:]
    except Exception as e:
        print(f"[state_manager] Failed to read progress: {e}")
        return []
