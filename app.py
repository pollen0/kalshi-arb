#!/usr/bin/env python3
"""
Kalshi Arbitrage Dashboard - Main Entry Point

Usage:
    python app.py              # Start web dashboard on port 5050
    python app.py --port 8080  # Custom port
"""

import signal
import subprocess
import sys
import os

# Add src to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def kill_existing(port: int):
    """Kill any process already listening on the given port."""
    try:
        result = subprocess.run(
            ["lsof", "-ti", f":{port}"],
            capture_output=True, text=True, timeout=5,
        )
        pids = result.stdout.strip().split()
        if not pids or pids == ['']:
            return
        my_pid = str(os.getpid())
        for pid in pids:
            if pid and pid != my_pid:
                print(f"[STARTUP] Killing stale process on port {port}: PID {pid}")
                os.kill(int(pid), signal.SIGTERM)
        # Brief wait for process to exit
        import time
        time.sleep(0.5)
    except Exception as e:
        print(f"[STARTUP] Could not check port {port}: {e}")


def main():
    # Railway/Heroku set PORT env var; fall back to 5050 for local dev
    port = int(os.environ.get('PORT', 5050))

    # Parse command line args (override env var)
    for i, arg in enumerate(sys.argv):
        if arg == '--port' and i + 1 < len(sys.argv):
            port = int(sys.argv[i + 1])

    # Kill any existing instance on this port
    kill_existing(port)

    print(f"""
    ╔═══════════════════════════════════════════════════════╗
    ║           KALSHI ARBITRAGE DASHBOARD                  ║
    ╠═══════════════════════════════════════════════════════╣
    ║  Starting server on http://localhost:{port}             ║
    ║  Press Ctrl+C to stop                                 ║
    ╚═══════════════════════════════════════════════════════╝
    """)

    # Use gunicorn in production if available, fall back to Flask dev server
    try:
        import gunicorn  # noqa: F401
        from gunicorn.app.base import BaseApplication

        class StandaloneApplication(BaseApplication):
            def __init__(self, options=None):
                self.options = options or {}
                super().__init__()

            def load_config(self):
                for key, value in self.options.items():
                    if key in self.cfg.settings and value is not None:
                        self.cfg.set(key.lower(), value)

            def load(self):
                # create_app() runs HERE, inside the worker process (after fork).
                # This ensures background threads start in the worker, not the master.
                from src.web.app import create_app
                return create_app()

        options = {
            'bind': f'0.0.0.0:{port}',
            'workers': 1,          # Single worker (background threads share state)
            'threads': 4,          # Handle concurrent dashboard requests
            'timeout': 120,        # Long timeout for slow API calls
        }
        print("[STARTUP] Using gunicorn production server")
        StandaloneApplication(options).run()
    except ImportError:
        print("[STARTUP] gunicorn not installed, using Flask dev server (not for production)")
        from src.web.app import create_app
        app = create_app()
        app.run(host='0.0.0.0', port=port, debug=False)


if __name__ == '__main__':
    main()
