# pull-requests-collector

`pull_requests_collector.py` collects pull requests from GitHub, Gitea or
Forgejo, resolves their dependencies, and cherry-picks them, in dependency
order, onto an existing Git tree. It is a single stdlib-only Python script
(Python 3.10+; Git 2.38+ is needed for `--dry-run`).

It is meant to be wrapped by a CI action: all logs go to stderr, and a single
JSON document goes to stdout.

## What it does

1. Parse each `--query` into server-side and client-side filters.
2. List matching PRs (`/issues`), re-checking labels, base branch and draft
   state client-side because servers silently ignore unknown labels.
3. Resolve the issue-dependency graph (BFS) and topologically sort it,
   dependencies first. A cycle is a fatal error.
4. For each PR in order:
   - skip it if the server reports it unmergeable;
   - fetch `refs/pull/N/head` from the PR's own repository;
   - select the proposed commits: the topmost run of commits sharing the
     newest commit's `BugLink:` set (older commits are rebase artifacts);
     the newest commit must have a BugLink;
   - `git cherry-pick -x -s` them. On failure all commits of that PR are
     rolled back.
5. Print the JSON result.

With `--dry-run` step 4 is simulated in memory (`git merge-tree` +
`git commit-tree`): index, worktree and HEAD stay untouched, but conflicts,
including conflicts between PRs of the same run, are still detected.

## Platforms

| Platform        | API base                                  |
| --------------- | ----------------------------------------- |
| github.com      | `https://api.github.com`                  |
| GitHub Ent. S.  | `<server>/api/v3`                         |
| Gitea / Forgejo | `<server>/api/v1` (subpath installs work) |

`--platform auto` picks GitHub for github.com, otherwise probes
`<server>/api/v1/version` (a 401/403 still counts as "Gitea/Forgejo"), and
falls back to GitHub Enterprise. With `auto`, Forgejo is reported as
`gitea`; an explicit `--platform` value is reported as given.

Notes: GitHub allows one milestone per query (names are translated to
numbers) and has no `q` filter. GitHub reports `mergeable: null` while it is
computing; that is treated as mergeable.

Token scopes on Gitea/Forgejo: `read:repository` plus `read:issue` (the
script uses `/issues` and `/issues/N/dependencies`).

## Committer identity

The cherry-picks are committed, and signed off (`-s`), as:

1. `--committer-name` / `--committer-email`, if given;
2. else the repository's own `user.name` / `user.email` Git config;
3. else the committer of the current HEAD commit.

The identity is passed as `git -c` settings; nothing is written to the
repository config. Set `user.name`/`user.email` in the target tree, or pass
the flags, to control the `Signed-off-by:` trailer.

## Interface

```sh
pull_requests_collector.py --server-url URL --query QUERY [--query QUERY ...]
    [--token TOKEN] [--platform auto|github|gitea|forgejo]
    [--working-directory DIR] [--committer-name N] [--committer-email E]
    [--skip-on-error] [--dry-run]
```

| Option                | Default | Description                                       |
| --------------------- | ------- | ------------------------------------------------- |
| `--server-url`        | —       | e.g. `https://github.com`, `https://host/forgejo` |
| `--query`             | —       | repeatable; results of all queries are unioned    |
| `--token`             | empty   | API and Git fetch token                           |
| `--platform`          | `auto`  | force the API flavour                             |
| `--working-directory` | `.`     | existing Git tree to apply onto                   |
| `--committer-name`    | (above) | committer name override                           |
| `--committer-email`   | (above) | committer email override                          |
| `--skip-on-error`     | off     | keep going after a failed PR (default: stop)      |
| `--dry-run`           | off     | simulate, leave the tree untouched                |

### Query syntax

```text
owner/repo [key:value ...]
```

Filters within one query are ANDed; separate `--query` flags are unioned.
Values containing spaces are not supported.

| Filter             | Side   | Notes                                    |
| ------------------ | ------ | ---------------------------------------- |
| `state`            | server | default `open`                           |
| `label` / `labels` | both   | comma-separated, all required            |
| `milestone`        | server | name; one only on GitHub                 |
| `author`           | server | Gitea `created_by`, GitHub `creator`     |
| `assignee`         | server |                                          |
| `q`                | server | free text; Gitea/Forgejo only            |
| `base`             | client | target branch                            |
| `ready`            | client | `true` = not draft, `false` = draft only |

### Output (stdout)

```json
{
  "dry_run": false,
  "platform": "gitea",
  "enumerated": ["example/firmware#123"],
  "applied": ["example/firmware#123"],
  "skipped": [],
  "failed": [],
  "titles": { "example/firmware#123": "Update foo firmware blob" },
  "buglinks": {
    "example/firmware#123": ["https://bugs.launchpad.net/bugs/1000001"]
  },
  "buglinks_grouped": {
    "https://bugs.launchpad.net/bugs/1000001": ["example/firmware#123"]
  },
  "ref": "aaaa1111aaaa1111aaaa1111aaaa1111aaaa1111"
}
```

| Field              | Description                                                |
| ------------------ | ---------------------------------------------------------- |
| `dry_run`          | boolean; when true, `applied` means "would apply"          |
| `platform`         | detected or given platform (`auto` gives `github`/`gitea`) |
| `enumerated`       | all PRs in apply order, including unmergeable ones and     |
|                    | ones never reached because an earlier PR failed            |
| `applied`          | PRs cherry-picked (or simulated in dry-run)                |
| `skipped`          | PRs skipped because the server marks them unmergeable      |
| `failed`           | PRs with a conflict or no BugLink on the newest commit     |
| `titles`           | PR ref to title                                            |
| `buglinks`         | PR ref to its BugLink URLs                                 |
| `buglinks_grouped` | BugLink URL to the PRs sharing it                          |
| `ref`              | HEAD SHA of the tree (unchanged in dry-run)                |

### Exit status

| Code | Meaning                                                       |
| ---- | ------------------------------------------------------------- |
| 0    | no PR failed (unmergeable skips are not failures)             |
| 1    | at least one PR failed, or a fatal error (API, query, cycle); |
|      | on fatal errors nothing is printed to stdout                  |

Check the exit code first, then parse stdout. Warnings and errors on stderr
use the `::warning::` / `::error::` annotation format.

## Example

Collect PRs labelled `stable` that are not drafts, or that are also labelled
`wanted/experimental`, onto `origin/stable`:

```sh
git clone https://forge.example.com/example/firmware.git tests
cd tests
git checkout -b pr-collector-test origin/stable

pull_requests_collector.py \
  --server-url https://forge.example.com \
  --token "$TOKEN" \
  --query 'example/firmware label:stable ready:true' \
  --query 'example/firmware label:stable,wanted/experimental'
```

Resulting tree (`git log --format=fuller -1`):

```text
commit aaaa1111aaaa1111aaaa1111aaaa1111aaaa1111
Author:     Alice Author <alice@example.com>
AuthorDate: Fri Oct 30 09:46:40 2026 +0000
Commit:     Carol Collector <carol@example.com>
CommitDate: Tue Nov 17 15:50:18 2026 +0000

    foo: update firmware for FOO1234 device

    BugLink: https://bugs.launchpad.net/bugs/1000001

    Update binary firmware for FOO1234 devices
    ...
    Signed-off-by: Alice Author <alice@example.com>
    (cherry picked from commit bbbb2222bbbb2222bbbb2222bbbb2222bbbb2222)
    Signed-off-by: Bob Maintainer <bob@example.com>
    (cherry picked from commit cccc3333cccc3333cccc3333cccc3333cccc3333)
    Signed-off-by: Carol Collector <carol@example.com>
```

```sh
$ git log --graph --format='%h %an | %cn %s' -2
* aaaa1111a Alice Author | Carol Collector foo: update firmware for FOO1234 device
*   dddd4444d Dave Dev | Dave Dev Merge pull request '...' (#42) ... into stable
```

Each cherry-pick keeps the original author, gains a `(cherry picked from
commit ...)` line (`-x`) and a trailing `Signed-off-by:` of the committer.

## Tests

```sh
python3 -m pytest tests
```
