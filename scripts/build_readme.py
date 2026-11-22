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
SSL_CONTEXT = ssl.create_default_context()

FRONT_MATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*(?:\n|$)", re.S)
REPOSITORY_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
STAR_LINE_RE = re.compile(
    r"^- \[.*?\]\(https://github\.com/([^)]+)\) — .*?⭐ ([\d,]+)", re.M
)
MARKDOWN_SPECIAL_RE = re.compile(r"([\\\[\]*_`<>])")

REQUIRED_FIELDS = ("name", "category", "repository")
OPTIONAL_FIELDS = ("subcategory", "description")
# GitHub REST allows 60 unauthenticated requests/hour; leave headroom.
MAX_UNAUTHENTICATED_REQUESTS = 50
BATCH_SIZE = 40


def read_tools():
    tools = []
    seen = {}
    for path in sorted(TOOLS_DIR.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        match = FRONT_MATTER_RE.search(text)
        if not match:
            raise ValueError(f"Missing front matter in {path}")
        fields = {}
        for line in match.group(1).splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, sep, value = line.partition(":")
            key = key.strip()
            if not sep or not key:
                raise ValueError(f"Malformed front-matter line in {path}: {line!r}")
            if key in fields:
                raise ValueError(f"Duplicate field {key!r} in {path}")
            fields[key] = value.strip().strip('"')
        for required in REQUIRED_FIELDS:
            if not fields.get(required):
                raise ValueError(f"Missing {required} in {path}")
        unknown = set(fields) - set(REQUIRED_FIELDS) - set(OPTIONAL_FIELDS)
        if unknown:
            raise ValueError(f"Unknown field(s) in {path}: {', '.join(sorted(unknown))}")
        repository = fields["repository"]
        if not REPOSITORY_RE.fullmatch(repository):
            raise ValueError(f"Invalid repository {repository!r} in {path}; expected owner/name")
        canonical = repository.lower()
        if canonical in seen:
            raise ValueError(f"Duplicate repository {repository} in {seen[canonical]} and {path}")
        seen[canonical] = path
        for optional in OPTIONAL_FIELDS:
            fields.setdefault(optional, "")
        tools.append(fields)
    return tools


def open_with_retry(request, attempts=3):
    """Retry transient GitHub failures and secondary rate limits."""
    for attempt in range(attempts):
        try:
            return urllib.request.urlopen(request, timeout=30, context=SSL_CONTEXT)
        except urllib.error.HTTPError as exc:
            if exc.code not in (403, 429, 500, 502, 503, 504) or attempt == attempts - 1:
                raise
        except urllib.error.URLError:
            if attempt == attempts - 1:
                raise
        time.sleep(2**attempt)
    raise AssertionError("unreachable")


def graphql_stars(tools, token):
    """Fetch stars in batches, avoiding REST's unauthenticated low rate limit."""
    stars = {}
    for start in range(0, len(tools), BATCH_SIZE):
        batch = tools[start : start + BATCH_SIZE]
        aliases = []
        requested = {}
        for index, tool in enumerate(batch):
            owner, name = tool["repository"].split("/", 1)
            alias = f"r{index}"
            requested[alias] = tool["repository"].lower()
            aliases.append(
                f'{alias}: repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) '
                "{ nameWithOwner stargazerCount }"
            )
        query = "query { " + " ".join(aliases) + " }"
        payload = json.dumps({"query": query}).encode()
        request = urllib.request.Request(
            "https://api.github.com/graphql",
            data=payload,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "User-Agent": "resource-readme-builder",
            },
        )
        with open_with_retry(request) as response:
            data = json.load(response)
        # A renamed/private/deleted repository comes back as null, sometimes with
        # an error in `errors`. Keep the rest of the batch and resolve the gaps
        # through REST later, which follows repository redirects.
        for error in data.get("errors") or []:
            location = ".".join(str(part) for part in error.get("path", []))
            print(
                f"GitHub GraphQL warning ({location or 'query'}): "
                f"{error.get('message', 'unknown error')}",
                file=sys.stderr,
            )
        result = data.get("data") or {}
        for alias, value in result.items():
            if not value:
                continue
            count = int(value["stargazerCount"])
            stars[value["nameWithOwner"].lower()] = count
            if alias in requested:
                stars[requested[alias]] = count
        if not result:
            raise RuntimeError(f"GitHub GraphQL returned no data for batch starting at {start}")
    return stars


def rest_stars(tools, token=None):
    """REST fallback: follows renamed repositories and handles small catalogs."""
    stars = {}
    for tool in tools:
        repository = tool["repository"]
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "resource-readme-builder"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request("https://api.github.com/repos/" + repository, headers=headers)
        try:
            with open_with_retry(request) as response:
                data = json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                print(f"GitHub REST: {repository} not found; keeping a live badge", file=sys.stderr)
                continue
            raise
        count = int(data["stargazers_count"])
        stars[repository.lower()] = count
        stars[data["full_name"].lower()] = count
        time.sleep(0.05)
    return stars


def stars_from_readme(tools):
    """Reuse counts already present in README.md so offline runs stay stable."""
    if not README.exists():
        return {}
    stars = {}
    for match in STAR_LINE_RE.finditer(README.read_text(encoding="utf-8")):
        stars[match.group(1).lower()] = int(match.group(2).replace(",", ""))
    return stars


def fetch_stars(tools, offline=False):
    if offline:
        return stars_from_readme(tools)
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        stars = graphql_stars(tools, token)
        missing = [tool for tool in tools if tool["repository"].lower() not in stars]
        if missing:
            print(f"Resolving {len(missing)} repository(ies) via REST.", file=sys.stderr)
            stars.update(rest_stars(missing, token))
        return stars
    print("No GitHub token found; using REST API (limited to 60 requests/hour).", file=sys.stderr)
    if len(tools) > MAX_UNAUTHENTICATED_REQUESTS:
        raise SystemExit(
            f"{len(tools)} repositories exceed the unauthenticated REST budget "
            f"({MAX_UNAUTHENTICATED_REQUESTS}); set GITHUB_TOKEN and try again."
        )
    return rest_stars(tools)


def slugify(value):
    return re.sub(r"[^a-z0-9 -]", "", value.casefold()).replace(" ", "-")


def md_escape(value):
    return MARKDOWN_SPECIAL_RE.sub(r"\\\1", value)


def make_readme(tools, stars):
    categories = defaultdict(list)
    for tool in tools:
        tool["stars"] = stars.get(tool["repository"].lower())
        categories[tool["category"]].append(tool)
    category_names = sorted(categories, key=str.casefold)
    anchors = {name: slugify(name) for name in category_names}
    if len(set(anchors.values())) != len(anchors):
        raise ValueError("Category names produce duplicate anchors; rename a category")

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
        " | ".join(f"[{name}](#{anchors[name]})" for name in category_names),
        "",
    ]

    for category in category_names:
        lines.extend([
            f'<a id="{anchors[category]}"></a>',
            "<details open>",
            f"<summary>{md_escape(category)} ({len(categories[category])})</summary>",
            "",
        ])
        grouped = defaultdict(list)
        for tool in categories[category]:
            grouped[tool["subcategory"]].append(tool)
        for subcategory in sorted(grouped, key=str.casefold):
            if subcategory:
                lines.extend([f"#### {md_escape(subcategory)}", ""])
            for tool in sorted(grouped[subcategory], key=lambda t: (-(t["stars"] or 0), t["name"].casefold())):
                desc = f" — {md_escape(tool['description'])}" if tool["description"] else ""
                repository = tool["repository"]
                tag = f"![Latest tag](https://img.shields.io/github/v/tag/{repository})"
                release = f"![Latest release](https://img.shields.io/github/v/release/{repository})"
                star_badge = (
                    f"⭐ {tool['stars']:,}"
                    if tool["stars"] is not None
                    else f"![GitHub stars](https://img.shields.io/github/stars/{repository}?style=flat)"
                )
                lines.append(
                    f"- [{md_escape(tool['name'])}](https://github.com/{repository}) "
                    f"— {tag} {release} {star_badge}{desc}"
                )
            lines.append("")
        lines.extend(["</details>", ""])

    lines.extend([
        "## Add a tool with Copilot",
        "",
        "Use the `add-tool-to-catalog` skill in Copilot Chat. Paste a GitHub repository URL and ask, for example: `Use the add-tool-to-catalog skill to add https://github.com/owner/repo.` It checks for duplicates, selects a category and subcategory, creates the tool record, and validates it.",
        "",
        "See the [skill instructions](.github/skills/add-tool-to-catalog/SKILL.md).",
        "",
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
    parser.add_argument("--offline", action="store_true", help="reuse star counts from README.md instead of querying GitHub")
    parser.add_argument("--validate", action="store_true", help="validate tool records without writing README.md")
    args = parser.parse_args()
    tools = read_tools()
    if not tools:
        raise SystemExit("No Markdown tool records found in tools/.")
    if args.validate:
        print(f"Validated {len(tools)} tool records in {len(set(t['category'] for t in tools))} categories.")
        return
    stars = fetch_stars(tools, offline=args.offline)
    README.write_text(make_readme(tools, stars), encoding="utf-8")
    print(f"Updated {README.name} with {len(tools)} repositories in {len(set(t['category'] for t in tools))} categories.")


if __name__ == "__main__":
    main()
