#!/usr/bin/env python3
"""
O-RAN-SC LOC Statistics Tool
Collects lines-added, lines-deleted, net change, and total LOC
for a list of O-RAN-SC GitHub repos over a specified date range.

Usage:
    python oran_sc_loc_stats.py \
        --token YOUR_GITHUB_TOKEN \
        --repos repo1 repo2 repo3 \
        --since 2024-01-01 \
        --until 2024-12-31

Requirements:
    pip install requests tabulate pygount
"""

import argparse
import sys
import os
import tempfile
import shutil
import subprocess
from datetime import datetime, timezone
from collections import defaultdict

try:
    import requests
except ImportError:
    sys.exit("Missing dependency: pip install requests")

try:
    from tabulate import tabulate
except ImportError:
    sys.exit("Missing dependency: pip install tabulate")

# ─────────────────────────── Config ───────────────────────────

ORG = "o-ran-sc"
GITHUB_API = "https://api.github.com"


# ─────────────────────────── GitHub helpers ───────────────────

def gh_session(token: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    return s


def check_rate_limit(session: requests.Session):
    r = session.get(f"{GITHUB_API}/rate_limit")
    r.raise_for_status()
    data = r.json()["resources"]["core"]
    remaining = data["remaining"]
    reset_ts = datetime.fromtimestamp(data["reset"])
    print(f"  [rate limit] {remaining} requests remaining (resets at {reset_ts:%H:%M:%S})")
    if remaining < 10:
        print("  ⚠  WARNING: very few API requests remaining. Consider waiting before re-running.")


def list_org_repos(session: requests.Session) -> list[str]:
    """Return all repo names in the O-RAN-SC org."""
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
        repos.extend(repo["name"] for repo in batch)
        page += 1
    return repos


def get_commits(session: requests.Session, repo: str, since: str, until: str) -> list[dict]:
    """Fetch all commits in the repo between since..until (ISO 8601)."""
    commits, page = [], 1
    while True:
        r = session.get(
            f"{GITHUB_API}/repos/{ORG}/{repo}/commits",
            params={"since": since, "until": until, "per_page": 100, "page": page},
        )
        if r.status_code == 409:          # empty repo
            break
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        commits.extend(batch)
        page += 1
    return commits


def get_commit_stats(session: requests.Session, repo: str, sha: str) -> tuple[int, int]:
    """Return (additions, deletions) for a single commit."""
    r = session.get(f"{GITHUB_API}/repos/{ORG}/{repo}/commits/{sha}")
    r.raise_for_status()
    stats = r.json().get("stats", {})
    return stats.get("additions", 0), stats.get("deletions", 0)


# ─────────────────────────── Total-LOC via git clone ──────────

def count_total_loc(repo: str, token: str) -> int:
    """
    Clone the repo at HEAD and count total lines using pygount or wc -l fallback.
    Returns 0 if clone fails or pygount/wc are unavailable.
    """
    tmpdir = tempfile.mkdtemp(prefix="oran_loc_")
    clone_url = f"https://{token}@github.com/{ORG}/{repo}.git"
    try:
        result = subprocess.run(
            ["git", "clone", "--depth=1", "--quiet", clone_url, tmpdir],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            print(f"    ⚠  git clone failed for {repo}: {result.stderr.strip()[:80]}")
            return 0

        # Try pygount first (language-aware, ignores binaries)
        try:
            import pygount
            analysis = pygount.SourceAnalysis.from_file
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

        # Fallback: wc -l on all non-binary files
        try:
            result2 = subprocess.run(
                'find . -not -path "./.git/*" -type f | xargs grep -Il "" | xargs wc -l 2>/dev/null | tail -1',
                shell=True, capture_output=True, text=True, cwd=tmpdir,
            )
            line = result2.stdout.strip().split()[0] if result2.stdout.strip() else "0"
            return int(line)
        except Exception:
            return 0
    except subprocess.TimeoutExpired:
        print(f"    ⚠  clone timed out for {repo}")
        return 0
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ─────────────────────────── Display helpers ──────────────────

def fmt_num(n: int) -> str:
    return f"{n:,}"


def sign(n: int) -> str:
    return f"+{n:,}" if n >= 0 else f"{n:,}"


def print_banner(since: str, until: str, repos: list[str]):
    print()
    print("╔══════════════════════════════════════════════════════════════╗")
    print("║          O-RAN-SC  —  LOC Statistics Report                 ║")
    print("╚══════════════════════════════════════════════════════════════╝")
    print(f"  Organization : {ORG}")
    print(f"  Date range   : {since[:10]}  →  {until[:10]}")
    print(f"  Repos        : {len(repos)}")
    print()


def print_results(rows: list[dict], include_total_loc: bool):
    if not rows:
        print("  No data collected.")
        return

    # Sort by net change descending
    rows.sort(key=lambda r: r["net"], reverse=True)

    headers = ["Repository", "Commits", "Lines Added", "Lines Deleted", "Net Change"]
    if include_total_loc:
        headers.append("Total LOC (HEAD)")

    table = []
    for r in rows:
        row = [
            r["repo"],
            fmt_num(r["commits"]),
            fmt_num(r["added"]),
            fmt_num(r["deleted"]),
            sign(r["net"]),
        ]
        if include_total_loc:
            row.append(fmt_num(r["total_loc"]) if r["total_loc"] > 0 else "—")
        table.append(row)

    # Totals row
    totals = [
        "TOTAL",
        fmt_num(sum(r["commits"]  for r in rows)),
        fmt_num(sum(r["added"]    for r in rows)),
        fmt_num(sum(r["deleted"]  for r in rows)),
        sign(sum(r["net"]         for r in rows)),
    ]
    if include_total_loc:
        total_loc_sum = sum(r["total_loc"] for r in rows)
        totals.append(fmt_num(total_loc_sum) if total_loc_sum > 0 else "—")
    table.append(["─" * 30] + ["─" * 10] * (len(headers) - 1))
    table.append(totals)

    print(tabulate(table, headers=headers, tablefmt="rounded_outline", stralign="right",
                   colalign=("left",) + ("right",) * (len(headers) - 1)))
    print()


# ─────────────────────────── Main ─────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="Collect LOC stats from O-RAN-SC GitHub repos.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Stats for three repos in Q1 2024
  python oran_sc_loc_stats.py \\
      --token ghp_xxxx \\
      --repos o-du-low o-du-high o-cu-cp \\
      --since 2024-01-01 --until 2024-03-31

  # All repos in the org (slow — many API calls)
  python oran_sc_loc_stats.py \\
      --token ghp_xxxx --all-repos \\
      --since 2024-01-01 --until 2024-06-30

  # Include total LOC count at HEAD (requires git; pygount recommended)
  python oran_sc_loc_stats.py \\
      --token ghp_xxxx --repos o-du-low \\
      --since 2024-01-01 --until 2024-12-31 \\
      --total-loc
        """,
    )
    p.add_argument("--token",    required=True, help="GitHub personal access token (needs repo read access)")
    p.add_argument("--repos",    nargs="+",     help="Repo names (without org prefix)", metavar="REPO")
    p.add_argument("--all-repos",action="store_true", help="Process every repo in the o-ran-sc org")
    p.add_argument("--since",    required=True, help="Start date  YYYY-MM-DD")
    p.add_argument("--until",    required=True, help="End date    YYYY-MM-DD")
    p.add_argument("--total-loc",action="store_true",
                   help="Also count total LOC at HEAD via git clone (slower; requires git + optionally pygount)")
    return p.parse_args()


def to_iso(date_str: str, end_of_day: bool = False) -> str:
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    if end_of_day:
        dt = dt.replace(hour=23, minute=59, second=59)
    return dt.isoformat().replace("+00:00", "Z")


def main():
    args = parse_args()

    if not args.repos and not args.all_repos:
        sys.exit("Provide --repos REPO1 REPO2 ... or --all-repos")

    since_iso = to_iso(args.since)
    until_iso = to_iso(args.until, end_of_day=True)

    session = gh_session(args.token)

    # Resolve repo list
    if args.all_repos:
        print(f"Fetching repo list for {ORG} org …")
        repos = list_org_repos(session)
        print(f"  Found {len(repos)} repos.")
    else:
        repos = args.repos

    check_rate_limit(session)
    print_banner(args.since, args.until, repos)

    rows = []
    for idx, repo in enumerate(repos, 1):
        print(f"[{idx}/{len(repos)}] {repo}")

        # 1. Commits in range
        print(f"    Fetching commits …", end=" ", flush=True)
        commits = get_commits(session, repo, since_iso, until_iso)
        print(f"{len(commits)} found")

        if not commits:
            rows.append({"repo": repo, "commits": 0, "added": 0, "deleted": 0, "net": 0, "total_loc": 0})
            continue

        # 2. Per-commit stats (additions + deletions)
        added = deleted = 0
        for ci, commit in enumerate(commits, 1):
            sha = commit["sha"]
            print(f"    Commit stats {ci}/{len(commits)} …\r", end="", flush=True)
            a, d = get_commit_stats(session, repo, sha)
            added += a
            deleted += d
        print(" " * 40 + "\r", end="")  # clear progress line
        print(f"    +{added:,} added  -{deleted:,} deleted  net {added - deleted:+,}")

        # 3. Optional: total LOC at HEAD
        total_loc = 0
        if args.total_loc:
            print(f"    Cloning to count total LOC …", end=" ", flush=True)
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

    print()
    print("━" * 66)
    print("  RESULTS")
    print("━" * 66)
    print_results(rows, include_total_loc=args.total_loc)


if __name__ == "__main__":
    main()
