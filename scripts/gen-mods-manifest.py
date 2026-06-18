#!/usr/bin/env python3
"""Walk mods/*.pw.toml, enrich each entry with Modrinth (or CurseForge,
when Modrinth doesn't have it) metadata, and emit a mods.json that the
website renders on /mods.

The website used to ship a hand-curated mods.json; on every release this
script regenerates it from the current packwiz state so the mods list is
always exactly what the modpack contains. CF-only entries (e.g. mods
imported via `packwiz cf detect` because no Modrinth equivalent existed)
get their name/description/icon from CF's public API; CF_API_KEY is
required in env for that fallback to fire.
"""
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

MODS_DIR = Path("mods")
OUTPUT = Path("mods.json")
OVERRIDES_FILE = Path("data/mod-categories.json")

# Modrinth's category vocabulary -> our /mods page buckets.
# Modrinth tags are returned per-project as a list of strings; first hit wins.
CATEGORY_MAP = {
    "library": "lib",
    "optimization": "perf",
    "decoration": "build",
    "mobs": "mobs",
    "worldgen": "world",
    "adventure": "core",
    "magic": "core",
    "food": "core",
    "storage": "core",
    "transportation": "build",
    "technology": "core",
    "equipment": "qol",
    "game-mechanics": "qol",
    "utility": "qol",
    "management": "qol",
    "social": "qol",
}


def parse_pw_toml(path: Path) -> dict:
    text = path.read_text()
    fn = re.search(r'^filename = "(.+)"', text, re.M)
    side = re.search(r'^side = "(.+)"', text, re.M)
    url = re.search(r'^url = "(.+)"', text, re.M)
    cf_pid = re.search(
        r'^\[update\.curseforge\][\s\S]*?^project-id\s*=\s*(\d+)',
        text,
        re.M,
    )
    return {
        "filename": fn.group(1) if fn else path.stem,
        "side": side.group(1) if side else "both",
        "url": url.group(1) if url else None,
        "cf_project_id": int(cf_pid.group(1)) if cf_pid else None,
    }


def extract_modrinth_id(url: str | None) -> str | None:
    if not url:
        return None
    m = re.search(r"cdn\.modrinth\.com/data/([^/]+)/", url)
    return m.group(1) if m else None


def fetch_modrinth_projects(ids: list[str]) -> list[dict]:
    """GET /v2/projects?ids=[...] — Modrinth supports up to ~1000 ids per call.
    We have ~150 so one batch is enough."""
    if not ids:
        return []
    qs = urllib.parse.urlencode({"ids": json.dumps(ids)})
    req = urllib.request.Request(
        f"https://api.modrinth.com/v2/projects?{qs}",
        headers={"User-Agent": "beyond-adventures-modpack/1.0"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def fetch_cf_mods(ids: list[int]) -> list[dict]:
    """POST /v1/mods with body {modIds: [...]} — used for the few CF-only mods
    that don't have a Modrinth listing. Silently no-ops if CF_API_KEY is unset."""
    if not ids:
        return []
    api_key = os.environ.get("CF_API_KEY", "").strip()
    if not api_key:
        print("CF_API_KEY not set — skipping CF fallback", file=sys.stderr)
        return []
    req = urllib.request.Request(
        "https://api.curseforge.com/v1/mods",
        data=json.dumps({"modIds": ids}).encode(),
        headers={
            "x-api-key": api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)["data"]


def map_category(p: dict) -> str:
    cats = (p.get("categories") or []) + (p.get("additional_categories") or [])
    for c in cats:
        if c in CATEGORY_MAP:
            return CATEGORY_MAP[c]
    return "other"


def pw_stem(path: Path) -> str:
    """Strip both .toml and .pw suffixes — Path.stem only drops the last one,
    so atmosfera-neo.pw.toml -> atmosfera-neo.pw without this."""
    stem = path.stem
    return stem[:-3] if stem.endswith(".pw") else stem


def load_overrides() -> dict:
    if not OVERRIDES_FILE.exists():
        return {}
    raw = json.loads(OVERRIDES_FILE.read_text())
    return {k: v for k, v in raw.items() if not k.startswith("_")}


def main():
    overrides = load_overrides()
    entries = []
    modrinth_ids = []
    for path in sorted(MODS_DIR.glob("*.pw.toml")):
        pw = parse_pw_toml(path)
        mid = extract_modrinth_id(pw["url"])
        entries.append({"path": path, "pw": pw, "modrinth_id": mid})
        if mid:
            modrinth_ids.append(mid)

    print(f"{len(entries)} pw.tomls; {len(modrinth_ids)} have a Modrinth id", file=sys.stderr)

    projects = fetch_modrinth_projects(modrinth_ids)
    by_modrinth_id = {p["id"]: p for p in projects}
    print(f"Modrinth returned {len(projects)} project records", file=sys.stderr)

    # Anything Modrinth couldn't account for: fall back to CF if a project-id
    # is in the [update.curseforge] block. teamsmod and similar custom mods
    # have no CF entry either and stay filename-derived.
    cf_lookup_ids = [
        e["pw"]["cf_project_id"]
        for e in entries
        if e["pw"]["cf_project_id"]
        and (not e["modrinth_id"] or e["modrinth_id"] not in by_modrinth_id)
    ]
    cf_mods = fetch_cf_mods(cf_lookup_ids)
    by_cf_id = {m["id"]: m for m in cf_mods}
    print(f"CurseForge returned {len(cf_mods)} project records", file=sys.stderr)

    result = []
    for entry in entries:
        mid = entry["modrinth_id"]
        mr = by_modrinth_id.get(mid) if mid else None
        cf_id = entry["pw"]["cf_project_id"]
        cf = by_cf_id.get(cf_id) if cf_id else None

        if mr:
            slug = mr.get("slug") or ""
            category = overrides.get(slug) or map_category(mr)
            result.append({
                "slug": mr["id"],
                "name": mr["title"],
                "authors": None,
                "description": mr.get("description"),
                "icon": mr.get("icon_url"),
                "category": category,
                "modrinthSlug": slug,
                "url": f"https://modrinth.com/mod/{mr['id']}",
            })
        elif cf:
            stem = pw_stem(entry["path"])
            logo = (cf.get("logo") or {}).get("thumbnailUrl") or (cf.get("logo") or {}).get("url")
            website = (cf.get("links") or {}).get("websiteUrl")
            category = overrides.get(stem, "other")
            result.append({
                "slug": stem,
                "name": cf.get("name") or stem,
                "authors": None,
                "description": cf.get("summary"),
                "icon": logo,
                "category": category,
                "modrinthSlug": None,
                "url": website,
            })
        else:
            # Custom build / maven mod (teamsmod) — best-effort filename label.
            stem = pw_stem(entry["path"])
            name = re.sub(r"[-_]", " ", stem).strip().title()
            category = overrides.get(stem, "other")
            result.append({
                "slug": stem,
                "name": name,
                "authors": None,
                "description": None,
                "icon": None,
                "category": category,
                "modrinthSlug": None,
                "url": None,
            })

    result.sort(key=lambda m: (m["category"], m["name"].lower()))
    OUTPUT.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    print(f"Wrote {OUTPUT} with {len(result)} mods", file=sys.stderr)


if __name__ == "__main__":
    main()
