"""Publish a fully staged native broker without modifying an executable in use."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile


def digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


def publish(source, out, native, rustc):
    out = Path(out)
    temporary = provenance = None
    try:
        with tempfile.NamedTemporaryFile(dir=out.parent, prefix='.frameatlas-cloud-next-', delete=False) as handle:
            temporary = Path(handle.name)
        shutil.copy2(source, temporary)
        with temporary.open('r+b') as stream:
            os.fsync(stream.fileno())
        pin = {'broker_sha256': digest(temporary), 'filmcraft_binary': str(native),
               'filmcraft_sha256': digest(native), 'rustc': rustc}
        with tempfile.NamedTemporaryFile(dir=out.parent, prefix='.frameatlas-provenance-next-',
                                         mode='w', delete=False) as handle:
            provenance = Path(handle.name)
            handle.write(json.dumps(pin, indent=2))
            handle.flush()
            os.fsync(handle.fileno())
        # Open transfers retain their old executable inode; new jobs see a complete build.
        os.replace(temporary, out)
        os.replace(provenance, out.parent / 'build-provenance.json')
        return pin
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        if provenance is not None:
            provenance.unlink(missing_ok=True)
