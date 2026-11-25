"""Single-process entry point for hosted demos (Streamlit Community Cloud, Hugging Face Spaces).

Starts the FastAPI backend in a background thread on 127.0.0.1:8000 (once per container),
then runs the normal dashboard in frontend/app.py. Locally you can still use run_local.sh.
"""
import os
import runpy
import socket
import sys
import threading
import time

import streamlit as st

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _api_up() -> bool:
    s = socket.socket()
    s.settimeout(0.5)
    try:
        s.connect(("127.0.0.1", 8000))
        return True
    except OSError:
        return False
    finally:
        s.close()


@st.cache_resource(show_spinner=False)
def _start_api() -> bool:
    if _api_up():
        return True
    if not (os.path.exists(os.path.join(ROOT, "data", "samples", "invoices_open.json"))):
        runpy.run_path(os.path.join(ROOT, "scripts", "generate_samples.py"), run_name="__main__")
    import uvicorn

    server = uvicorn.Server(uvicorn.Config("backend.main:app", host="127.0.0.1", port=8000, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(240):  # first start trains the delay model; allow up to 2 minutes
        if _api_up():
            return True
        time.sleep(0.5)
    return False


_start_api()
runpy.run_path(os.path.join(ROOT, "frontend", "app.py"), run_name="__main__")
