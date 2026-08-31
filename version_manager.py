#!/usr/bin/env python3
"""
Version manager for 大眼X Guardian.
Manages version archives, rollback, and state hashing.
"""
import hashlib
import json
import os
import shutil
import sys
import time

# Paths
FROZEN = getattr(sys, 'frozen', False)
BASE_DIR = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
ROOT_DIR = os.path.dirname(sys.executable) if FROZEN else os.path.dirname(os.path.abspath(__file__))

VERSIONS_DIR = os.path.join(ROOT_DIR, "data", "versions")
MANIFEST_FILE = os.path.join(ROOT_DIR, "data", "version_manifest.json")

# Files to include in version archives
ARCHIVE_FILES = [
    "server.py",
    "rpc_server.py",
    "guardian.py",
    "db.py",
    "llm.py",
    "config.py",
    "desktop.py",
    "public/index.html",
    "public/icon.png",
]


def _ensure_versions_dir():
    """Ensure versions directory exists."""
    if not os.path.exists(VERSIONS_DIR):
        os.makedirs(VERSIONS_DIR, exist_ok=True)


def _hash_file(filepath):
    """Compute SHA256 hash of a file."""
    if not os.path.exists(filepath):
        return None
    try:
        sha256 = hashlib.sha256()
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                sha256.update(chunk)
        return sha256.hexdigest()
    except Exception:
        return None


def _load_manifest():
    """Load version manifest."""
    if not os.path.exists(MANIFEST_FILE):
        return {}
    try:
        with open(MANIFEST_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_manifest(manifest):
    """Save version manifest."""
    os.makedirs(os.path.dirname(MANIFEST_FILE), exist_ok=True)
    try:
        with open(MANIFEST_FILE, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[version_manager] Failed to save manifest: {e}")


def compute_state_hash():
    """Compute hash of current state (all tracked files)."""
    hashes = {}
    for rel_path in ARCHIVE_FILES:
        full_path = os.path.join(ROOT_DIR, rel_path)
        h = _hash_file(full_path)
        if h:
            hashes[rel_path] = h
    
    # Combine all hashes
    combined = "|".join(f"{k}:{v}" for k, v in sorted(hashes.items()))
    return hashlib.sha256(combined.encode()).hexdigest()


def has_changes():
    """Check if current state differs from last archived state."""
    manifest = _load_manifest()
    last_hash = manifest.get("last_archive_hash")
    current_hash = compute_state_hash()
    return last_hash != current_hash


def archive(label="auto"):
    """Create a version archive."""
    _ensure_versions_dir()
    
    timestamp = int(time.time())
    version_name = f"v{timestamp}_{label}"
    archive_dir = os.path.join(VERSIONS_DIR, version_name)
    
    if os.path.exists(archive_dir):
        shutil.rmtree(archive_dir)
    os.makedirs(archive_dir)
    
    # Copy tracked files
    copied = []
    for rel_path in ARCHIVE_FILES:
        src = os.path.join(ROOT_DIR, rel_path)
        if os.path.exists(src):
            dst = os.path.join(archive_dir, rel_path)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            try:
                shutil.copy2(src, dst)
                copied.append(rel_path)
            except Exception as e:
                print(f"[version_manager] Failed to copy {rel_path}: {e}")
    
    # Save metadata
    metadata = {
        "version": version_name,
        "timestamp": timestamp,
        "label": label,
        "files": copied,
        "hash": compute_state_hash(),
    }
    
    meta_file = os.path.join(archive_dir, "metadata.json")
    with open(meta_file, "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2, ensure_ascii=False)
    
    # Update manifest
    manifest = _load_manifest()
    manifest["last_archive_hash"] = metadata["hash"]
    manifest["last_archive_version"] = version_name
    manifest["last_archive_time"] = timestamp
    
    # Store guardian hash if available
    guardian_path = os.path.join(ROOT_DIR, "guardian.py")
    guardian_hash = _hash_file(guardian_path)
    if guardian_hash:
        manifest["guardian_hash"] = guardian_hash
    
    _save_manifest(manifest)
    
    print(f"[version_manager] Archived {version_name} ({len(copied)} files)")
    return version_name


def list_archives():
    """List all available version archives."""
    _ensure_versions_dir()
    
    archives = []
    if not os.path.exists(VERSIONS_DIR):
        return archives
    
    for entry in os.listdir(VERSIONS_DIR):
        archive_dir = os.path.join(VERSIONS_DIR, entry)
        if not os.path.isdir(archive_dir):
            continue
        
        meta_file = os.path.join(archive_dir, "metadata.json")
        if not os.path.exists(meta_file):
            continue
        
        try:
            with open(meta_file, "r", encoding="utf-8") as f:
                metadata = json.load(f)
            archives.append({
                "version": metadata.get("version", entry),
                "timestamp": metadata.get("timestamp", 0),
                "label": metadata.get("label", ""),
                "files": metadata.get("files", []),
            })
        except Exception:
            pass
    
    # Sort by timestamp descending
    archives.sort(key=lambda x: x.get("timestamp", 0), reverse=True)
    return archives


def rollback(version):
    """Rollback to a specific version."""
    archive_dir = os.path.join(VERSIONS_DIR, version)
    if not os.path.exists(archive_dir):
        return False, []
    
    meta_file = os.path.join(archive_dir, "metadata.json")
    if not os.path.exists(meta_file):
        return False, []
    
    try:
        with open(meta_file, "r", encoding="utf-8") as f:
            metadata = json.load(f)
    except Exception:
        return False, []
    
    files_to_restore = metadata.get("files", [])
    restored = []
    
    for rel_path in files_to_restore:
        src = os.path.join(archive_dir, rel_path)
        dst = os.path.join(ROOT_DIR, rel_path)
        
        if not os.path.exists(src):
            continue
        
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            restored.append(rel_path)
        except Exception as e:
            print(f"[version_manager] Failed to restore {rel_path}: {e}")
    
    print(f"[version_manager] Rolled back to {version} ({len(restored)} files restored)")
    return True, restored


def get_current_info():
    """Get current version information."""
    manifest = _load_manifest()
    
    return {
        "current_hash": compute_state_hash(),
        "last_archive_version": manifest.get("last_archive_version"),
        "last_archive_time": manifest.get("last_archive_time"),
        "last_archive_hash": manifest.get("last_archive_hash"),
        "has_changes": has_changes(),
    }
