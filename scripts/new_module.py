"""Scaffold a new module from app/modules/_template and enable it in modules.yaml.

Usage:  python scripts/new_module.py <module_name>
"""
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULES_DIR = ROOT / "app" / "modules"
TEMPLATE_DIR = MODULES_DIR / "_template"
MODULES_FILE = ROOT / "modules.yaml"


def main() -> None:
    if len(sys.argv) != 2 or not re.fullmatch(r"[a-z][a-z0-9_]*", sys.argv[1]):
        sys.exit("Usage: python scripts/new_module.py <snake_case_name>")

    name = sys.argv[1]
    target = MODULES_DIR / name
    if target.exists():
        sys.exit(f"Module '{name}' already exists at {target}")

    shutil.copytree(TEMPLATE_DIR, target, ignore=shutil.ignore_patterns("__pycache__"))
    for file in target.glob("*.py"):
        file.write_text(file.read_text(encoding="utf-8").replace("__MODULE_NAME__", name), encoding="utf-8")

    with MODULES_FILE.open("a", encoding="utf-8") as f:
        f.write(f"\n  {name}:\n    enabled: true\n")

    print(f"Created module '{name}' at {target} and enabled it in modules.yaml")


if __name__ == "__main__":
    main()
