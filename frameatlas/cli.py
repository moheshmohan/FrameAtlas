import argparse
import json
from pathlib import Path
from .store import Store
from .service import Service

def main():
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["import-native", "import-takeout", "import-manifest", "search", "stats", "analyze", "models"])
    p.add_argument("value", nargs="?", default="")
    p.add_argument("--language", default="auto")
    args = p.parse_args()
    svc = Service(Store())
    actions = {"import-native": ("import_native", {}), "import-takeout": ("import_takeout", {"path": args.value}),
               "search": ("search", {"query": args.value}), "stats": ("stats", {}),
               "analyze": ("enqueue", {"id": args.value, "options": {"language": args.language}}), "models": ("models", {})}
    try:
        action, params = ("import_manifest", json.loads(Path(args.value).read_text())) if args.action == "import-manifest" else actions[args.action]
        print(json.dumps(svc.dispatch(action, params), ensure_ascii=False, indent=2))
    except Exception as exc:
        p.exit(1, "Error: " + str(exc) + "\n")

if __name__ == "__main__":
    main()
