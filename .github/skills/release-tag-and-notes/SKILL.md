---
name: release-tag-and-notes
description: Open a develop-to-master pull request, then tag the merge commit on master and publish a GitHub release with notes. Use when the user asks to release, cut a version, create tags, write release notes, or open a PR into master.
---

# Release: PR, tag, and release notes

Ship `develop` to `master`, then tag and publish a GitHub release. The repository is `prawee/resource`. Run `git` and `gh` write commands that need the network or `.git` access outside the sandbox only when required.

## Conventions (from existing history)

- PRs go `develop` → `master` (not `main`).
- Tags are `vMAJOR.MINOR.PATCH` (for example `v2.0.2`). Ignore the legacy `1.0.0` tag when picking the next version.
- Release title equals the tag name.
- Release notes use "What's Changed" and "Full Changelog" sections (see Release notes format).

## Workflow

1. **Check state.** Run `git status --short`, `git fetch origin --tags`, then list the commits to ship with `git log --oneline origin/master..origin/develop` and the diff with `git diff --stat origin/master...origin/develop`. Stop and report if `develop` has uncommitted or unpushed work. Do not commit or push unless the user asks.
2. **Open the PR** if none is open (`gh pr list --repo prawee/resource --head develop --base master --state open`):

   ```bash
   gh pr create --repo prawee/resource --base master --head develop --title "<title>" --body "<summary>"
   ```

   Summarize the changes grouped by category/subcategory. Report the PR URL.
3. **Merge only when authorized.** Do not merge unless the user says to or confirms they merged. Verify with:

   ```bash
   gh pr view <number> --repo prawee/resource --json state,mergedAt,mergeCommit
   ```

   If `state` is not `MERGED`, stop and ask. Tags and releases must come after the merge.
4. **Choose the version.** Look at `git tag --sort=-creatordate` and `gh release list --repo prawee/resource --limit 5`.
   - Patch bump for catalog entries, docs, and fixes.
   - Minor bump for structural changes (new tooling, workflow, or catalog format).
   - Major bump only for breaking changes.
   If the user delegates the choice, pick one and state the reasoning. Ask only if patch vs. minor is genuinely unclear.
5. **Create the tag and release in one step**, targeting the PR's merge commit rather than the moving tip of `master` (bots push "refresh repository stars" commits there):

   ```bash
   gh release create vX.Y.Z --repo prawee/resource --target <mergeCommit-oid> --title vX.Y.Z --notes "<notes>" --latest
   ```

   Never reuse or move an existing tag. If the tag exists, choose the next version or ask.
6. **Verify.** Confirm the release URL returned by `gh`, or run `gh release view vX.Y.Z --repo prawee/resource`.

## Release notes format

```markdown
## What's Changed
- <PR title> by @<author> in https://github.com/prawee/resource/pull/<number>
  - <Category / Subcategory>: <owner/repo>, <owner/repo>
  - <other notable change>

**Full Changelog**: https://github.com/prawee/resource/compare/<previous-tag>...<new-tag>
```

- List one bullet per merged PR, with sub-bullets grouping notable changes by category.
- Skip bot commits such as "chore: refresh repository stars".
- Base the notes only on the PR and the commits actually shipped.

## Completion

Report the PR URL, the tag and the commit it points to, the version choice with its rationale, and the release URL.
