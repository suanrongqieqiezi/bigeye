#!/usr/bin/env python3
"""
rpc_server.py — compatibility wrapper for guardian.py

Guardian expects rpc_server.py to have create_server() and main().
This module re-exports from the actual server module.
"""
import sys
import os

# Ensure project root is on path
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from server import create_server, main

if __name__ == "__main__":
    main()
