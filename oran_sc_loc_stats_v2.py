#!/usr/bin/env python3
"""
O-RAN-SC LOC Statistics Tool  v2
─────────────────────────────────
Automatically discovers every repo in the o-ran-sc org that had at least
one commit in the given date range, then collects:
  • lines added
  • lines deleted
  • net change (added − deleted)
  • total LOC at HEAD  (optional, via git clone)

Usage:
    python oran_sc_loc_stats_v2.py \
        --token YOUR_GITHUB_TOKEN \
        --since 2024-01-01 \
        --until 2024-12-31

    # Also count total LOC at HEAD for each active repo
    python oran_sc_loc_stats_v2.py \
        --token YOUR_GITHUB_TOKEN \
        --since 2024-01-01 --until 2024-12-31 \
        --total-loc

    # Exclude repos whose names match a pattern
    python oran_sc_loc_stats_v2.py \
        --token YOUR_GITHUB_TOKEN \
        --since 2024-01-01 --until 2024-12-31 \
        --exclude test-* deprecated-*

Requirements:
    pip install requests tabulate
    pip install pygount          # optional, for accurate total-LOC counting
"""

import argparse
import fnmatch
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

try:
    import requests
except ImportError:
    sys.exit("Missing dependency: pip install requests")

try:
    from tabulate import tabulate
except ImportError:
    sys.exit("Missing dependency: pip install tabulate")

# ─────────────────────────────────────────────────────────────
ORG        = "o-ran-sc"
GITHUB_API = "https://api.github.com"
# ─────────────────────────────────────────────────────────────


# ══════════════════════════ GitHub helpers ═══════════════════

def gh_session(token: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "Authorization":        f"Bearer {token}",
        "Accept":               "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    return s


def check_rate_limit(session: requests.Session) -> int:
    r = session.get(f"{GITHUB_API}/rate_limit")
    r.raise_for_status()
    data      = r.json()["resources"]["core"]
    remaining = data["remaining"]
    reset_ts  = datetime.fromtimestamp(data["reset"])
    status    = "⚠  WARNING: very few requests left!" if remaining < 50 else "OK"
    print(f"  [rate limit]  {remaining:,} requests remaining  "
          f"(resets {reset_ts:%H:%M:%S})  {status}")
    return remaining


def list_org_repos(session: requests.Session) -> list[dict]:
    """Return list of repo dicts (name, pushed_at, archived) for the org."""
    repos, page = [], 1
    while True:
        r = session.get(
            f"{GITHUB_API}/orgs/{ORG}/repos",
            params={"per_page": 100, "page": page, "type": "all"},
        )
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        repos.extend(batch)
        page += 1
    return repos


def repo_has_commits(session: requests.Session, repo: str,
                     since: str, until: str) -> bool:
    """
    Quick check: does the repo have ≥1 commit in [since, until]?
    Uses per_page=1 so it costs exactly one API call per repo.
    Returns False on 404 (inaccessible/deleted) or 409 (empty repo).
    """
    r = session.get(
        f"{GITHUB_API}/repos/{ORG}/{repo}/commits",
        params={"since": since, "until": until, "per_page": 1, "page": 1},
    )
    if r.status_code in (404, 409):
        return False
    r.raise_for_status()
    return len(r.json()) > 0


def get_commits(session: requests.Session, repo: str,
                since: str, until: str) -> list[dict] | None:
    """
    Fetch ALL commits in [since, until] for the given repo.
    Returns None if the repo is inaccessible (404).
    Returns [] for empty/unborn repos (409).
    """
    commits, page = [], 1
    while True:
        r = session.get(
            f"{GITHUB_API}/repos/{ORG}/{repo}/commits",
            params={"since": since, "until": until,
                    "per_page": 100, "page": page},
        )
        if r.status_code == 404:
            return None
        if r.status_code == 409:
            return []
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        commits.extend(batch)
        page += 1
    return commits


def get_commit_stats(session: requests.Session,
                     repo: str, sha: str) -> tuple[int, int]:
    """Return (additions, deletions) for a single commit SHA. Returns (0, 0) on 404."""
    r = session.get(f"{GITHUB_API}/repos/{ORG}/{repo}/commits/{sha}")
    if r.status_code == 404:
        return 0, 0
    r.raise_for_status()
    stats = r.json().get("stats", {})
    return stats.get("additions", 0), stats.get("deletions", 0)


# ══════════════════════════ Total-LOC (optional) ═════════════

def count_total_loc(repo: str, token: str) -> int:
    """
    Shallow-clone the repo and count total lines.
    Uses pygount (language-aware) if available, else falls back to wc -l.
    Returns 0 on any error.
    """
    tmpdir    = tempfile.mkdtemp(prefix="oran_loc_")
    clone_url = f"https://{token}@github.com/{ORG}/{repo}.git"
    try:
        res = subprocess.run(
            ["git", "clone", "--depth=1", "--quiet", clone_url, tmpdir],
            capture_output=True, text=True, timeout=180,
        )
        if res.returncode != 0:
            print(f"    ⚠  clone failed: {res.stderr.strip()[:80]}")
            return 0

        # ── pygount (preferred) ──────────────────────────────
        try:
            import pygount
            total = 0
            for root, _, files in os.walk(tmpdir):
                if ".git" in root:
                    continue
                for fname in files:
                    fpath = os.path.join(root, fname)
                    try:
                        sa = pygount.SourceAnalysis.from_file(fpath, "pygount")
                        if sa.state == pygount.SourceState.analyzed:
                            total += sa.code + sa.documentation + sa.empty
                    except Exception:
                        pass
            return total
        except ImportError:
            pass

        # ── wc -l fallback ───────────────────────────────────
        try:
            res2 = subprocess.run(
                'find . -not -path "./.git/*" -type f '
                '| xargs grep -Il "" | xargs wc -l 2>/dev/null | tail -1',
                shell=True, capture_output=True, text=True, cwd=tmpdir,
            )
            parts = res2.stdout.strip().split()
            return int(parts[0]) if parts else 0
        except Exception:
            return 0

    except subprocess.TimeoutExpired:
        print(f"    ⚠  clone timed out for {repo}")
        return 0
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ══════════════════════════ Display helpers ═══════════════════

def fmt(n: int) -> str:
    return f"{n:,}"

def signed(n: int) -> str:
    return f"+{n:,}" if n >= 0 else f"{n:,}"

def matches_any(name: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(name, p) for p in patterns)

def print_banner(since: str, until: str, total_repos: int,
                 active_repos: int, skipped_archived: int,
                 skipped_inactive: int, skipped_excluded: int):
    print()
    print("╔══════════════════════════════════════════════════════════════╗")
    print("║        O-RAN-SC  —  LOC Statistics Report  v2               ║")
    print("╚══════════════════════════════════════════════════════════════╝")
    print(f"  Organization      : {ORG}")
    print(f"  Date range        : {since}  →  {until}")
    print(f"  Total repos found : {total_repos}")
    if skipped_archived:
        print(f"  Skipped (archived): {skipped_archived}")
    if skipped_excluded:
        print(f"  Skipped (excluded): {skipped_excluded}")
    print(f"  Inactive (no commits in range): {skipped_inactive}")
    print(f"  Active repos (processing)     : {active_repos}")
    print()

def print_results(rows: list[dict], include_total_loc: bool):
    if not rows:
        print("  No active repos found in the given date range.")
        return

    rows.sort(key=lambda r: r["net"], reverse=True)

    headers = ["Repository", "Commits", "Lines Added", "Lines Deleted", "Net Change"]
    if include_total_loc:
        headers.append("Total LOC (HEAD)")

    table = []
    for r in rows:
        row = [
            r["repo"],
            fmt(r["commits"]),
            fmt(r["added"]),
            fmt(r["deleted"]),
            signed(r["net"]),
        ]
        if include_total_loc:
            row.append(fmt(r["total_loc"]) if r["total_loc"] > 0 else "—")
        table.append(row)

    # Separator + totals
    col_sep = ["─" * 32] + ["─" * 12] * (len(headers) - 1)
    totals  = [
        f"TOTAL  ({len(rows)} repos)",
        fmt(sum(r["commits"]  for r in rows)),
        fmt(sum(r["added"]    for r in rows)),
        fmt(sum(r["deleted"]  for r in rows)),
        signed(sum(r["net"]   for r in rows)),
    ]
    if include_total_loc:
        s = sum(r["total_loc"] for r in rows)
        totals.append(fmt(s) if s > 0 else "—")

    table.append(col_sep)
    table.append(totals)

    print(tabulate(
        table, headers=headers,
        tablefmt="rounded_outline",
        colalign=("left",) + ("right",) * (len(headers) - 1),
    ))
    print()


# ══════════════════════════ CLI ═══════════════════════════════

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Collect LOC stats from ALL active o-ran-sc repos "
            "in a date range (no repo list needed)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # All active repos in 2024
  python oran_sc_loc_stats_v2.py \\
      --token ghp_xxxx \\
      --since 2024-01-01 --until 2024-12-31

  # All active repos + total LOC at HEAD
  python oran_sc_loc_stats_v2.py \\
      --token ghp_xxxx \\
      --since 2024-01-01 --until 2024-12-31 \\
      --total-loc

  # Skip archived repos AND repos matching a shell glob
  python oran_sc_loc_stats_v2.py \\
      --token ghp_xxxx \\
      --since 2024-01-01 --until 2024-12-31 \\
      --skip-archived \\
      --exclude test-* deprecated-* tmp-*
        """,
    )
    p.add_argument("--token",       required=True,
                   help="GitHub PAT with repo read access")
    p.add_argument("--since",       required=True, metavar="YYYY-MM-DD",
                   help="Start of date range (inclusive)")
    p.add_argument("--until",       required=True, metavar="YYYY-MM-DD",
                   help="End of date range (inclusive)")
    p.add_argument("--skip-archived", action="store_true",
                   help="Skip repos marked as archived on GitHub")
    p.add_argument("--exclude",     nargs="+", default=[], metavar="PATTERN",
                   help="Shell-glob patterns for repo names to exclude "
                        "(e.g. test-* deprecated-*)")
    p.add_argument("--total-loc",   action="store_true",
                   help="Count total LOC at HEAD via git clone "
                        "(slower; requires git; pygount recommended)")
    return p.parse_args()


def to_iso(date_str: str, end_of_day: bool = False) -> str:
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    if end_of_day:
        dt = dt.replace(hour=23, minute=59, second=59)
    return dt.isoformat().replace("+00:00", "Z")


# ══════════════════════════ Main ══════════════════════════════

def main():
    args = parse_args()

    since_iso = to_iso(args.since)
    until_iso = to_iso(args.until, end_of_day=True)

    session = gh_session(args.token)

    # ── 1. Fetch all repos in the org ───────────────────────
    print(f"\nFetching repo list for '{ORG}' org …", end=" ", flush=True)
    all_repos = list_org_repos(session)
    print(f"{len(all_repos)} repos found.")

    check_rate_limit(session)

    # ── 2. Apply static filters (archived, exclude globs) ───
    skipped_archived = skipped_excluded = 0
    candidate_repos  = []

    for repo in all_repos:
        name = repo["name"]
        # Skip repos flagged as archived on GitHub
        if args.skip_archived and repo.get("archived"):
            skipped_archived += 1
            continue
        # Always skip repos whose name starts with "archived" (o-ran-sc convention)
        if name.startswith("archived"):
            skipped_archived += 1
            continue
        if matches_any(name, args.exclude):
            skipped_excluded += 1
            continue
        candidate_repos.append(name)

    # ── 3. Probe each candidate for commits in range ─────────
    print(f"\nChecking {len(candidate_repos)} repos for activity "
          f"between {args.since} and {args.until} …")

    active_repos     = []
    skipped_inactive = 0

    for idx, name in enumerate(candidate_repos, 1):
        print(f"  [{idx:>3}/{len(candidate_repos)}] {name:<50}", end="\r", flush=True)
        if repo_has_commits(session, name, since_iso, until_iso):
            active_repos.append(name)
        else:
            skipped_inactive += 1

    print(" " * 70 + "\r", end="")   # clear progress line
    print(f"  → {len(active_repos)} repos had commits in range "
          f"({skipped_inactive} inactive, skipped).")

    print_banner(
        args.since, args.until,
        total_repos      = len(all_repos),
        active_repos     = len(active_repos),
        skipped_archived = skipped_archived,
        skipped_inactive = skipped_inactive,
        skipped_excluded = skipped_excluded,
    )

    if not active_repos:
        print("  Nothing to process. Try a wider date range.")
        return

    # ── 4. Collect detailed stats for each active repo ───────
    rows = []
    for idx, repo in enumerate(active_repos, 1):
        print(f"[{idx}/{len(active_repos)}] {repo}")

        # Commits (full list — we already know there's ≥1)
        print("    Fetching commits …", end=" ", flush=True)
        commits = get_commits(session, repo, since_iso, until_iso)

        if commits is None:
            print("SKIPPED (404 — repo not found or inaccessible)")
            continue

        print(f"{len(commits)} commits")

        # Per-commit additions/deletions
        added = deleted = 0
        for ci, commit in enumerate(commits, 1):
            print(f"    Commit stats {ci}/{len(commits)} …\r", end="", flush=True)
            a, d = get_commit_stats(session, repo, commit["sha"])
            added   += a
            deleted += d
        print(" " * 50 + "\r", end="")
        print(f"    +{added:,} added  −{deleted:,} deleted  "
              f"net {added - deleted:+,}")

        # Optional total LOC
        total_loc = 0
        if args.total_loc:
            print("    Cloning for total LOC …", end=" ", flush=True)
            total_loc = count_total_loc(repo, args.token)
            print(f"{total_loc:,} lines")

        rows.append({
            "repo":      repo,
            "commits":   len(commits),
            "added":     added,
            "deleted":   deleted,
            "net":       added - deleted,
            "total_loc": total_loc,
        })

    # ── 5. Print results ─────────────────────────────────────
    print()
    print("━" * 66)
    print("  RESULTS")
    print("━" * 66)
    print_results(rows, include_total_loc=args.total_loc)


if __name__ == "__main__":
    main()
