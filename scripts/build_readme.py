#!/usr/bin/env python3
"""Build the repository catalog README from Markdown tool records."""

from __future__ import annotations

import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
import argparse
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR = ROOT / "tools"
README = ROOT / "README.md"
SYSTEM_CA = Path("/etc/ssl/cert.pem")
SSL_CONTEXT = ssl.create_default_context(cafile=str(SYSTEM_CA) if SYSTEM_CA.exists() else None)


def read_tools():
    tools = []
    for path in sorted(TOOLS_DIR.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        match = re.search(r"^---\s*\n(.*?)\n---\s*(?:\n|$)", text, re.S)
        if not match:
            raise ValueError(f"Missing front matter in {path}")
        fields = {}
        for line in match.group(1).splitlines():
            key, sep, value = line.partition(":")
            if sep:
                fields[key.strip()] = value.strip().strip('"')
        for required in ("name", "category", "repository"):
            if not fields.get(required):
                raise ValueError(f"Missing {required} in {path}")
        fields["subcategory"] = fields.get("subcategory", "")
        fields["description"] = fields.get("description", "")
        tools.append(fields)
    return tools


def graphql_stars(tools, token):
    """Fetch stars in batches, avoiding REST's unauthenticated low rate limit."""
    stars = {}
    for start in range(0, len(tools), 40):
        batch = tools[start : start + 40]
        aliases = []
        for index, tool in enumerate(batch):
            owner, name = tool["repository"].split("/", 1)
            aliases.append(f'r{index}: repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) {{ nameWithOwner stargazerCount }}')
        query = "query { " + " ".join(aliases) + " }"
        payload = json.dumps({"query": query}).encode()
        request = urllib.request.Request(
            "https://api.github.com/graphql",
            data=payload,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json", "User-Agent": "resource-readme-builder"},
        )
        with urllib.request.urlopen(request, timeout=30, context=SSL_CONTEXT) as response:
            data = json.load(response)
        if data.get("errors"):
            raise RuntimeError(f"GitHub GraphQL error: {data['errors'][0].get('message', 'unknown error')}")
        for value in data["data"].values():
            if value:
                stars[value["nameWithOwner"].lower()] = int(value["stargazerCount"])
        missing = [t["repository"] for t in batch if t["repository"].lower() not in stars]
        if missing:
            raise RuntimeError("Repositories not found on GitHub: " + ", ".join(missing))
    return stars


def rest_stars(tools, token):
    """Unauthenticated fallback for small catalogs; a token is recommended."""
    stars = {}
    for tool in tools:
        url = "https://api.github.com/repos/" + tool["repository"]
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "resource-readme-builder"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=30, context=SSL_CONTEXT) as response:
            stars[tool["repository"].lower()] = int(json.load(response)["stargazers_count"])
        time.sleep(0.05)
    return stars


def fetch_stars(tools, offline=False):
    if offline:
        return {tool["repository"].lower(): None for tool in tools}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        return graphql_stars(tools, token)
    print("No GitHub token found; using REST API (limited to 60 requests/hour).", file=sys.stderr)
    return rest_stars(tools, None)


def make_readme(tools, stars):
    categories = defaultdict(list)
    for tool in tools:
        tool["stars"] = stars[tool["repository"].lower()]
        categories[tool["category"]].append(tool)
    category_names = sorted(categories, key=str.casefold)

    lines = [
        "# Resource",
        "",
        "My tools that I use and research.",
        "",
        "A curated catalog of useful tools and open source projects. Each entry lives in its own Markdown file under [`tools/`](tools/), so the list can grow without a database.",
        "",
        "## Overview",
        "",
        "The chart shows the number of projects in each category. Star badges are live; GitHub Actions fetches numeric counts daily and sorts entries by stars within each category.",
        "",
        "```mermaid",
        "xychart-beta horizontal",
        '    x-axis "Category" [' + ", ".join(json.dumps(name) for name in category_names) + "]",
        '    y-axis "Repositories" 0 --> ' + str(max(1, max(len(categories[c]) for c in category_names))),
        "    bar [" + ", ".join(str(len(categories[c])) for c in category_names) + "]",
        "```",
        "",
        "## Categories",
        "",
        " | ".join(f"[{name}](#{re.sub(r'[^a-z0-9 -]', '', name.casefold()).replace(' ', '-')})" for name in category_names),
        "",
    ]

    for category in category_names:
        lines.extend([f"## {category}", ""])
        grouped = defaultdict(list)
        for tool in categories[category]:
            grouped[tool["subcategory"]].append(tool)
        for subcategory in sorted(grouped, key=str.casefold):
            if subcategory:
                lines.extend([f"### {subcategory}", ""])
            for tool in sorted(grouped[subcategory], key=lambda t: (-(t["stars"] or 0), t["name"].casefold())):
                desc = f" — {tool['description']}" if tool["description"] else ""
                stars = f"⭐ {tool['stars']:,}" if tool["stars"] is not None else f"![GitHub stars](https://img.shields.io/github/stars/{tool['repository']}?style=flat)"
                lines.append(f"- [{tool['name']}](https://github.com/{tool['repository']}) — {stars}{desc}")
            lines.append("")

    lines.extend([
        "## Add a tool",
        "",
        "Create a Markdown file in `tools/` with this front matter, then run `python3 scripts/build_readme.py`:",
        "",
        "```markdown",
        "---",
        'name: "Example Tool"',
        'category: "Developer Tools"',
        'subcategory: "Optional group"',
        'repository: "owner/repository"',
        'description: "Optional short description"',
        "---",
        "",
        "Notes about the tool can go here.",
        "```",
        "",
        "GitHub stars are refreshed automatically every day by GitHub Actions. To refresh and sort locally, set `GITHUB_TOKEN` and run `python3 scripts/build_readme.py`.",
        "",
    ])
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true", help="generate the README without querying GitHub (stars remain live badges)")
    args = parser.parse_args()
    tools = read_tools()
    if not tools:
        raise SystemExit("No Markdown tool records found in tools/.")
    stars = fetch_stars(tools, offline=args.offline)
    README.write_text(make_readme(tools, stars), encoding="utf-8")
    print(f"Updated {README.name} with {len(tools)} repositories in {len(set(t['category'] for t in tools))} categories.")


if __name__ == "__main__":
    main()
