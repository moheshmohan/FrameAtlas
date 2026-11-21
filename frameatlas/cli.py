import argparse
import json
from pathlib import Path
from .store import Store
from .service import Service

def main():
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["import-native", "import-takeout", "import-manifest", "search", "stats", "analyze", "models",
                                     "metadata-coverage", "metadata-backfill", "ideas", "ideas-add", "ideas-generate"])
    p.add_argument("value", nargs="?", default="")
    p.add_argument("--language", default="auto")
    p.add_argument("--count", type=int, default=5)
    p.add_argument("--since", default="")
    p.add_argument("--until", default="")
    p.add_argument("--model", default="")
    args = p.parse_args()
    svc = Service(Store())
    actions = {"import-native": ("import_native", {}), "import-takeout": ("import_takeout", {"path": args.value}),
               "search": ("search", {"query": args.value}), "stats": ("stats", {}),
               "analyze": ("enqueue", {"id": args.value, "options": {"language": args.language}}), "models": ("models", {}),
               "metadata-coverage": ("metadata_coverage", {}), "metadata-backfill": ("metadata_backfill", {}),
               "ideas": ("idea_pad", {}), "ideas-add": ("idea_pad_add", {"title": args.value}),
               "ideas-generate": ("idea_pad_generate", {"count": args.count, "since": args.since, "until": args.until,
                                                        "model": args.model, "focus": args.value})}
    try:
        action, params = ("import_manifest", json.loads(Path(args.value).read_text())) if args.action == "import-manifest" else actions[args.action]
        print(json.dumps(svc.dispatch(action, params), ensure_ascii=False, indent=2))
    except Exception as exc:
        p.exit(1, "Error: " + str(exc) + "\n")

if __name__ == "__main__":
    main()
