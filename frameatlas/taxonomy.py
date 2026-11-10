"""Conservative, explainable categories from local model output. No identity guesses."""
import re

RULES = {
    "Travel & places": ("airport", "airplane", "hotel", "train", "temple", "monument", "beach", "mountain", "street", "city"),
    "Food & dining": ("restaurant", "food", "meal", "dinner", "cooking", "cafe", "coffee", "eating"),
    "Tech & making": ("device", "electronic", "flipper", "chameleon", "card", "printer", "circuit", "computer", "keyboard"),
    "Cars & driving": ("car", "driving", "vehicle", "dashboard", "steering", "road", "traffic"),
    "People & moments": ("family", "child", "children", "baby", "people", "person", "man", "woman"),
    "Outdoors & adventure": ("paragliding", "hiking", "snow", "forest", "river", "park", "outdoor", "sky"),
    "Work & documents": ("office", "terminal", "container", "workplace", "document", "presentation"),
}


def classify(scenes, text):
    words = set(re.findall(r"\w+", text.casefold()))
    categories = [name for name, keys in RULES.items() if any(k in words for k in keys)]
    tags = set()
    for s in scenes:
        desc = s.get("description", {})
        for sub in desc.get("subjects", []):
            name = sub.get("name", "") if isinstance(sub, dict) else str(sub)
            if name.strip() and len(name.split()) <= 5:
                tags.add(name.strip().lower()[:50])
        for key in ("setting", "shotType"):
            val = desc.get(key)
            if isinstance(val, str) and val.strip() and len(val.split()) <= 3:
                tags.add(val.strip().lower()[:40])
    for keys in RULES.values():
        tags.update(k for k in keys if k in words)
    priority = {k for keys in RULES.values() for k in keys if k in words}
    return sorted(priority) + sorted(tags - priority)[:max(0, 24 - len(priority))], categories or ["Uncategorised"]
