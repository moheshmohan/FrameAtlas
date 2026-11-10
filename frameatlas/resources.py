"""Share the Mac GPU with FilmCraft renders and other local model jobs."""
import contextlib
import queue
import subprocess
import threading
import time
from .native import CLI


@contextlib.contextmanager
def gpu_lease(note="FrameAtlas local writing", timeout=120):
    p = subprocess.Popen([CLI, "locks", "hold", "gpu", "--minutes", "6", "--note", note], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    lines = queue.Queue(maxsize=100)
    def read():
        try:
            for line in p.stderr:
                lines.put(line, timeout=1)
        except (OSError, queue.Full):
            pass
    threading.Thread(target=read, daemon=True).start()
    try:
        deadline = time.monotonic() + timeout
        acquired = False
        while time.monotonic() < deadline:
            if p.poll() is not None:
                raise RuntimeError("Could not acquire the local GPU")
            try:
                if "locks hold: holding" in lines.get(timeout=0.25):
                    acquired = True
                    break
            except queue.Empty:
                pass
        if not acquired:
            raise RuntimeError("GPU busy with another local task. Retry after it finishes.")
        yield
    finally:
        if p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill(); p.wait(timeout=5)
