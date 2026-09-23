"""Refresh free provider metadata against a checked-out Leadpoet allowlist."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys


# Arena retains these operations for frozen historical submissions. This
# company-only source bundle must not advertise them to its research worker.
CONTACT_TOOLS = {
    "bounceban_get_single_status", "bounceban_verify_single", "datagma_find_email",
    "exa_people_search", "harvestapi_get_profile", "harvestapi_search_leads",
    "hunter_email_finder", "leadmagic_email_finder", "limadata_find_work_email",
    "zerobounce_validate",
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("leadpoet_source", type=Path)
    args = parser.parse_args()
    sys.path.insert(0, str(args.leadpoet_source.resolve()))
    from lab_arena.operations import DEEPLINE_TOOLS
    output = Path(__file__).resolve().parents[1] / "tyche_arena/catalog.json"
    tools = {}
    for name in DEEPLINE_TOOLS:
        if name in CONTACT_TOOLS:
            continue
        result = subprocess.run(["deepline", "tools", "describe", name, "--json"],
                                capture_output=True, text=True, timeout=45, check=True)
        row = next(json.loads(line) for line in reversed(result.stdout.splitlines()) if line.startswith("{"))
        if row.get("toolId") != name or not row.get("callable") or row.get("disabled"):
            raise ValueError(f"Unavailable tool: {name}")
        # Public contract only. Omit account/connection metadata and examples.
        tools[name] = {k: row[k] for k in ("toolId", "provider", "description", "inputSchema", "pricing", "callable") if k in row}
        if name == "exa_search":
            for field in tools[name]["inputSchema"].get("fields", []):
                if field.get("name") == "category":
                    field["type"] = " | ".join(
                        value.strip() for value in field["type"].split("|")
                        if value.strip() not in {'"people"', '"personal site"'}
                    )
            category = tools[name]["inputSchema"].get("jsonSchema", {}).get(
                "properties", {}
            ).get("category", {})
            if isinstance(category.get("enum"), list):
                category["enum"] = [
                    value for value in category["enum"]
                    if value not in {"people", "personal site"}
                ]
    revision = subprocess.check_output(["git", "-C", str(args.leadpoet_source), "rev-parse", "HEAD"], text=True).strip()
    output.write_text(json.dumps({"leadpoet_revision": revision,
        "retrieved_at": datetime.now(timezone.utc).isoformat(), "tools": tools}, indent=2) + "\n")
    print(f"Saved {len(tools)} approved tool contracts to {output}")


if __name__ == "__main__":
    main()
