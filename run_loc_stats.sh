#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# run_loc_stats.sh
# Batch runner for oran_sc_loc_stats.py (v1)
# Reads repo sets and time ranges from a YAML config file and runs the
# Python script once per entry, appending all output to a combined log.
#
# Usage:
#   ./run_loc_stats.sh [OPTIONS]
#
# Options:
#   -t TOKEN      GitHub personal access token (or set GITHUB_TOKEN env var)
#   -c CONFIG     Path to YAML config file       (default: oran_loc_config.yaml)
#   -s SCRIPT     Path to the v1 Python script   (default: oran_sc_loc_stats.py)
#   -o OUTPUT     Path to combined log file       (default: loc_stats_<timestamp>.log)
#   -d            Dry-run: print commands without executing them
#   -h            Show this help message
#
# Examples:
#   ./run_loc_stats.sh -t ghp_xxxx
#   ./run_loc_stats.sh -t ghp_xxxx -c my_config.yaml -o results.log
#   GITHUB_TOKEN=ghp_xxxx ./run_loc_stats.sh -c my_config.yaml
#   ./run_loc_stats.sh -t ghp_xxxx -d      # dry-run, nothing executed
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

# ── Defaults ────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="${SCRIPT_DIR}/oran_loc_config.yaml"
PY_SCRIPT="${SCRIPT_DIR}/oran_sc_loc_stats.py"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
LOG_FILE="${SCRIPT_DIR}/loc_stats_${TIMESTAMP}.log"
TOKEN="${GITHUB_TOKEN:-}"
DRY_RUN=false

# ── Colours ─────────────────────────────────────────────────────────────────
BOLD="\033[1m"
CYAN="\033[1;36m"
GREEN="\033[1;32m"
YELLOW="\033[1;33m"
RED="\033[1;31m"
RESET="\033[0m"

# ── Usage ────────────────────────────────────────────────────────────────────
usage() {
    grep '^#' "$0" | grep -v '^#!/' | sed 's/^# \{0,1\}//'
    exit 0
}

# ── Argument parsing ─────────────────────────────────────────────────────────
while getopts ":t:c:s:o:dh" opt; do
    case $opt in
        t) TOKEN="$OPTARG" ;;
        c) CONFIG="$OPTARG" ;;
        s) PY_SCRIPT="$OPTARG" ;;
        o) LOG_FILE="$OPTARG" ;;
        d) DRY_RUN=true ;;
        h) usage ;;
        :) echo -e "${RED}Error: -${OPTARG} requires an argument.${RESET}" >&2; exit 1 ;;
        \?) echo -e "${RED}Error: unknown option -${OPTARG}${RESET}" >&2; exit 1 ;;
    esac
done

# ── Validation ───────────────────────────────────────────────────────────────
if [[ -z "$TOKEN" ]]; then
    echo -e "${RED}Error: GitHub token required. Use -t TOKEN or set GITHUB_TOKEN.${RESET}" >&2
    exit 1
fi

if [[ ! -f "$CONFIG" ]]; then
    echo -e "${RED}Error: Config file not found: $CONFIG${RESET}" >&2
    exit 1
fi

if [[ ! -f "$PY_SCRIPT" ]]; then
    echo -e "${RED}Error: Python script not found: $PY_SCRIPT${RESET}" >&2
    exit 1
fi

# ── YAML parser (Python) ─────────────────────────────────────────────────────
# Emits one line per run in the format:
#   INDEX|NAME|SINCE|UNTIL|TOTAL_LOC|repo1,repo2,...
parse_config() {
    python3 - "$CONFIG" <<'EOF'
import sys, yaml

with open(sys.argv[1]) as f:
    cfg = yaml.safe_load(f)

runs = cfg.get("runs", [])
if not runs:
    print("ERROR: no 'runs' entries found in config", file=sys.stderr)
    sys.exit(1)

for i, run in enumerate(runs):
    name      = run.get("name", f"Run {i+1}").replace("|", "-")
    since     = run.get("since", "")
    until     = run.get("until", "")
    total_loc = "1" if run.get("total_loc", False) else "0"
    repos     = run.get("repos", [])

    if not since or not until:
        print(f"ERROR: run '{name}' is missing since/until", file=sys.stderr)
        sys.exit(1)
    if not repos:
        print(f"ERROR: run '{name}' has no repos", file=sys.stderr)
        sys.exit(1)

    repo_str = ",".join(repos)
    print(f"{i+1}|{name}|{since}|{until}|{total_loc}|{repo_str}")
EOF
}

# ── Logging helper ───────────────────────────────────────────────────────────
# Tees output to terminal AND log file, with a header per run.
log_run() {
    local run_index="$1"
    local run_name="$2"
    local run_cmd="$3"
    shift 3
    local cmd_args=("$@")

    local separator
    separator="$(printf '═%.0s' {1..70})"

    {
        echo ""
        echo "$separator"
        printf "RUN %s: %s\n" "$run_index" "$run_name"
        echo "COMMAND: $run_cmd"
        echo "STARTED: $(date '+%Y-%m-%d %H:%M:%S')"
        echo "$separator"
        echo ""
    } | tee -a "$LOG_FILE"

    local exit_code=0
    "${cmd_args[@]}" 2>&1 | tee -a "$LOG_FILE" || exit_code=$?

    {
        echo ""
        if [[ $exit_code -eq 0 ]]; then
            echo "✔  Run completed successfully."
        else
            echo "✘  Run exited with code $exit_code."
        fi
        echo "FINISHED: $(date '+%Y-%m-%d %H:%M:%S')"
        echo ""
    } | tee -a "$LOG_FILE"

    return $exit_code
}

# ── Main ─────────────────────────────────────────────────────────────────────
main() {
    # Write log file header
    if [[ "$DRY_RUN" == false ]]; then
        {
            echo "O-RAN-SC LOC Statistics — Batch Run"
            echo "Started  : $(date '+%Y-%m-%d %H:%M:%S')"
            echo "Config   : $CONFIG"
            echo "Script   : $PY_SCRIPT"
            echo "Log file : $LOG_FILE"
            echo ""
        } | tee "$LOG_FILE"
    fi

    echo -e "${CYAN}${BOLD}Parsing config: $CONFIG${RESET}"

    # Parse all runs
    mapfile -t run_lines < <(parse_config)

    total_runs="${#run_lines[@]}"
    echo -e "${CYAN}Found ${total_runs} run(s) in config.${RESET}"
    echo ""

    success_count=0
    fail_count=0

    for line in "${run_lines[@]}"; do
        IFS='|' read -r idx name since until total_loc repo_csv <<< "$line"

        # Convert comma-separated repos back to array
        IFS=',' read -ra repos <<< "$repo_csv"

        # Build the Python command
        cmd_args=(
            python3 "$PY_SCRIPT"
            --token "$TOKEN"
            --since "$since"
            --until "$until"
            --repos "${repos[@]}"
        )
        [[ "$total_loc" == "1" ]] && cmd_args+=(--total-loc)

        # Human-readable command string (token masked)
        cmd_display="python3 $(basename "$PY_SCRIPT") --token *** --since $since --until $until --repos ${repos[*]}"
        [[ "$total_loc" == "1" ]] && cmd_display+=" --total-loc"

        echo -e "${BOLD}[${idx}/${total_runs}] ${name}${RESET}"
        echo -e "  Repos : ${repos[*]}"
        echo -e "  Range : ${since}  →  ${until}"

        if [[ "$DRY_RUN" == true ]]; then
            echo -e "  ${YELLOW}[DRY RUN] Would execute: $cmd_display${RESET}"
            echo ""
            continue
        fi

        if log_run "$idx" "$name" "$cmd_display" "${cmd_args[@]}"; then
            echo -e "  ${GREEN}✔ Done${RESET}"
            (( success_count++ )) || true
        else
            echo -e "  ${RED}✘ Failed (see log for details)${RESET}"
            (( fail_count++ )) || true
        fi
        echo ""
    done

    # ── Summary ──────────────────────────────────────────────────────────────
    if [[ "$DRY_RUN" == true ]]; then
        echo -e "${YELLOW}Dry run complete — no scripts were executed.${RESET}"
        return 0
    fi

    {
        echo "════════════════════════════════════════════════════════════════════"
        echo "BATCH SUMMARY"
        echo "  Total runs : $total_runs"
        echo "  Succeeded  : $success_count"
        echo "  Failed     : $fail_count"
        echo "  Log file   : $LOG_FILE"
        echo "  Finished   : $(date '+%Y-%m-%d %H:%M:%S')"
        echo "════════════════════════════════════════════════════════════════════"
    } | tee -a "$LOG_FILE"

    if [[ $fail_count -gt 0 ]]; then
        exit 1
    fi
}

main
