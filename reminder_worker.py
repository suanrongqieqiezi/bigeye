#!/usr/bin/env python3
"""
Reminder worker — polls memory/reminders.json every second, fires due reminders
by inserting messages into the topic's chat DB.
"""
import json, os, time, threading, uuid

REMINDERS_FILE = None  # set by init()


def init(data_dir):
    global REMINDERS_FILE
    REMINDERS_FILE = os.path.join(data_dir, "reminders.json")
    if not os.path.exists(REMINDERS_FILE):
        with open(REMINDERS_FILE, "w", encoding="utf-8") as f:
            json.dump([], f)


def _load():
    try:
        with open(REMINDERS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _save(reminders):
    tmp = REMINDERS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(reminders, f, ensure_ascii=False, indent=2)
    os.replace(tmp, REMINDERS_FILE)


def add_reminder(topic_id, text, at_timestamp):
    """Add a reminder. Called by AI via write tool or API."""
    reminders = _load()
    reminders.append({
        "id": uuid.uuid4().hex[:12],
        "topic_id": topic_id,
        "text": text,
        "at": at_timestamp,
    })
    _save(reminders)


def start_worker(get_db_func):
    """Start background thread that fires due reminders."""
    def _loop():
        while True:
            try:
                now = time.time()
                reminders = _load()
                due = [r for r in reminders if r.get("at", 0) <= now]
                if due:
                    db = get_db_func()
                    for r in due:
                        try:
                            db.add_system_message(r["topic_id"], f"⏰ 提醒：{r['text']}")
                        except Exception:
                            pass
                    remaining = [r for r in reminders if r.get("at", 0) > now]
                    _save(remaining)
            except Exception:
                pass
            time.sleep(1)

    t = threading.Thread(target=_loop, daemon=True)
    t.start()
