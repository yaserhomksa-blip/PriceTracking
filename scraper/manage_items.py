"""Add or remove an item from items.json based on a GitHub issue.

Issue title: "Add: Snickers"   or   "Remove: Snickers"
Issue body (optional): "target: 5"
Prints the item name so the workflow can check just that item.
"""
import json
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
path = ROOT / "items.json"
items = json.loads(path.read_text(encoding="utf-8"))

title = os.environ.get("ISSUE_TITLE", "").strip()
body = os.environ.get("ISSUE_BODY", "") or ""
m = re.match(r"(add|remove)\s*:\s*(.+)", title, re.I)
if not m:
    raise SystemExit("Title must start with 'Add:' or 'Remove:'")
action, name = m.group(1).lower(), m.group(2).strip()

if action == "remove":
    items = [i for i in items if i["name"].lower() != name.lower()]
else:
    t = re.search(r"target\s*:\s*([0-9]+(?:\.[0-9]+)?)", body, re.I)
    words = [w for w in re.split(r"\s+", name.lower()) if w]
    item = {"name": name, "search": name, "require": [[w] for w in words]}
    if t:
        item["target"] = float(t.group(1))
    items = [i for i in items if i["name"].lower() != name.lower()] + [item]

path.write_text(json.dumps(items, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
with open(os.environ.get("GITHUB_OUTPUT", os.devnull), "a") as f:
    f.write(f"action={action}\nname={name}\n")
print(action, name)
