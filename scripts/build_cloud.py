"""Build only the native OAuth/Pickers broker, independently of editor/codec crates."""
from pathlib import Path
import os
import subprocess
import sys
from cloud_publish import publish

root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root))
from frameatlas.native import default_cli
native = Path(default_cli())
out = root / "cloud-bridge/bin/frameatlas-cloud"
out.parent.mkdir(parents=True, exist_ok=True)
command = ["cargo", "build", "--manifest-path", str(root / "cloud-bridge/Cargo.toml"), "--offline"]
r = subprocess.run(command, capture_output=True, text=True)
if r.returncode:
    print(r.stderr[-8000:], file=sys.stderr)
    print("Cloud broker build failed safely. A compatible sibling FilmCraft OAuth crate and cached Cargo dependencies are required. Local inventory and analysis remain available.", file=sys.stderr)
    sys.exit(1)
publish(root / 'cloud-bridge/target/debug/frameatlas-cloud', out, native,
        subprocess.check_output(['rustc', '--version'], text=True).strip())
print("Built secure cloud broker on FilmCraft OAuth. No model downloads.")
