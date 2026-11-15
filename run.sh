#!/bin/sh
# Start the 3D Models page on this machine only: http://127.0.0.1:8258/   (Ctrl-C stops it)
cd "$(dirname "$0")" && exec python3 server.py
