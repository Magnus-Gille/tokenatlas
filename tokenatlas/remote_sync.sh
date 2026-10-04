#!/usr/bin/env bash
# Pull the usage history of remote machines to this machine.
# Normally run through `tokenatlas collect --remote tag:host`; by hand or cron: */30 * * * * /path/to/remote_sync.sh
#
# Requires: SSH access to each remote host (see REMOTE_HOSTS below).
#
# Each remote machine that has tokenatlas (or the deprecated energy-monitor) installed snapshots its history DB,
# which is copied to ~/.local/state/tokenatlas/remote/<tag>.sqlite3 and merged with `tokenatlas import`
# (see docs/remote-machines.md). Hosts without it print "history: not installed on <tag>".
#
# This same script runs on multiple machines with different REMOTE_HOSTS, so the
# data flows as a mesh rather than only into one hub. Override the default host
# list via REMOTE_HOSTS_OVERRIDE (space-separated tag:host pairs), e.g. on m5's
# cron pulling only from the laptop:
#   REMOTE_HOSTS_OVERRIDE="laptop:magnus-macbook-air" /path/to/remote_sync.sh
#
# TOKENATLAS_DB (optional; ENERGY_MONITOR_DB is still honoured as a fallback): if set and non-empty,
# the local import goes into that database (`tokenatlas --db "$TOKENATLAS_DB" import ...`) instead of the default one, e.g. to try
# the sync without touching your real history.

# Bounded remote calls (a stalled host must never hang the run):
#   TOKENATLAS_SSH_OPTS     options for every ssh/scp call (default below)
#   TOKENATLAS_HOST_TIMEOUT overall seconds per host (default 300); a host over the limit is killed (with its
#                           children) and reported as "<host>: ERROR (timeout after Ns)", then the next host runs.
#                           The script then exits 1 once it has finished the remaining hosts.

set -euo pipefail

# Cron has a minimal PATH, and pipx/venv installs link tokenatlas into ~/.local/bin.
export PATH="$HOME/.local/bin:$PATH"

SSH_OPTS_STR="${TOKENATLAS_SSH_OPTS:--o ConnectTimeout=10 -o ServerAliveInterval=10 -o ServerAliveCountMax=3 -o BatchMode=yes}"
read -ra SSH_OPTS <<< "$SSH_OPTS_STR"
HOST_TIMEOUT="${TOKENATLAS_HOST_TIMEOUT:-300}"
if ! [[ "$HOST_TIMEOUT" =~ ^[0-9]+$ ]] || [[ "$HOST_TIMEOUT" -eq 0 ]]; then
    printf "TOKENATLAS_HOST_TIMEOUT must be a positive integer (seconds), got '%s'\n" "$HOST_TIMEOUT" >&2
    exit 2
fi

# tag:host pairs. Override a host via env var, e.g. PI_HOST=otherpi.local
DEFAULT_REMOTE_HOSTS=(
    "pi:${PI_HOST:-huginmunin.local}"
    "m5:${M5_HOST:-m5}"
)

if [[ -n "${REMOTE_HOSTS_OVERRIDE:-}" ]]; then
    read -ra REMOTE_HOSTS <<< "$REMOTE_HOSTS_OVERRIDE"
else
    REMOTE_HOSTS=("${DEFAULT_REMOTE_HOSTS[@]}")
fi

# Tags become file names and hosts become ssh/scp arguments: accept only plain characters and never a
# host that could be read as an option.  (ssh/scp get `--` before the host.)
valid_pair() {
    [[ "$1" =~ ^[A-Za-z0-9_-]+$ && "$2" =~ ^[A-Za-z0-9._@:-]+$ && "$2" != -* ]]
}

# Remote diagnostics are untrusted data: a compromised peer could emit terminal control sequences (clear screen, OSC 52
# clipboard writes, BEL, CR overwrites, C1 controls). terminal_safe shows every control byte as a visible \xNN escape:
# C0 (except tab; newline is the line separator), DEL, and every byte >= 0x80 (so C1 controls, raw or inside UTF-8, are
# neutralised; non-ASCII text is therefore shown as \xNN bytes rather than readable, which favours safety).
# Always print the result as a printf %s argument, never through echo (xpg_echo / POSIXLY_CORRECT would re-interpret \x escapes).
# Byte-wise sed under LC_ALL=C with a table built once, so it works on macOS (BSD) and Linux alike.
TERMINAL_SAFE_SED=""
build_terminal_safe_sed() {
    local i c
    for ((i = 1; i < 256; i++)); do
        if ((i == 9 || i == 10 || (i >= 32 && i < 127))); then continue; fi
        printf -v c '%b' "\\0$(printf '%03o' "$i")"
        TERMINAL_SAFE_SED+="$(printf 's/%s/\\\\x%02x/g' "$c" "$i")"$'\n'
    done
}
build_terminal_safe_sed
terminal_safe() {
    printf '%s\n' "$1" | LC_ALL=C sed -e "$TERMINAL_SAFE_SED"
}

# Merge the remote machine's history database (tokenatlas snapshot -> scp -> local import).
# Any failure is reported for this host only (return 1) and the loop continues with the next host.
# A remote without tokenatlas is a benign absence (return 0).
sync_history() {
    local tag="$1" host="$2"
    local state="$HOME/.local/state/tokenatlas" remote_dir
    remote_dir="$state/remote"
    # Non-interactive ssh has a minimal PATH too, so each remote call prepends ~/.local/bin.
    # Single quotes: the remote shell expands $HOME and ~, not this one.
    # Exit 1 from `command -v` means genuinely absent; anything else (255 = connection failure, ...) is an error.
    local probe_rc=0
    ssh "${SSH_OPTS[@]}" -- "$host" 'PATH="$HOME/.local/bin:$PATH"; command -v tokenatlas || command -v energy-monitor' >/dev/null 2>&1 || probe_rc=$?
    if [[ $probe_rc -eq 1 ]]; then
        echo "  history: not installed on $tag"
        return 0
    elif [[ $probe_rc -ne 0 ]]; then
        echo "  history: ERROR cannot check $tag for tokenatlas (ssh exit $probe_rc)" >&2
        return 1
    fi
    if ! command -v tokenatlas >/dev/null 2>&1; then
        echo "  history: ERROR tokenatlas is not installed locally" >&2
        return 1
    fi
    { mkdir -p "$remote_dir" && chmod 700 "$remote_dir"; } || { echo "  history: ERROR cannot create $remote_dir" >&2; return 1; }
    local err
    if ! err=$(ssh "${SSH_OPTS[@]}" -- "$host" 'PATH="$HOME/.local/bin:$PATH"; c=$(command -v tokenatlas || command -v energy-monitor) && "$c" snapshot ~/.local/state/tokenatlas/snapshot.sqlite3' 2>&1 >/dev/null); then
        printf '  history: ERROR snapshot failed on %s: %s\n' "$tag" "$(terminal_safe "$err")" >&2
        return 1
    fi
    if ! err=$(scp -q "${SSH_OPTS[@]}" -- "$host:.local/state/tokenatlas/snapshot.sqlite3" "$remote_dir/$tag.sqlite3.part" 2>&1); then
        rm -f -- "$remote_dir/$tag.sqlite3.part" 2>/dev/null || true
        printf '  history: ERROR scp failed for %s: %s\n' "$tag" "$(terminal_safe "$err")" >&2
        return 1
    fi
    # Under set -e an unguarded failure here would abort the whole loop, not just this host.
    if ! err=$(chmod 600 "$remote_dir/$tag.sqlite3.part" 2>&1 &&
               mv -f "$remote_dir/$tag.sqlite3.part" "$remote_dir/$tag.sqlite3" 2>&1); then
        rm -f -- "$remote_dir/$tag.sqlite3.part" 2>/dev/null || true
        printf '  history: ERROR cannot store snapshot for %s: %s\n' "$tag" "$(terminal_safe "$err")" >&2
        return 1
    fi
    local db_args=()
    local db="${TOKENATLAS_DB:-${ENERGY_MONITOR_DB:-}}"
    [[ -n "$db" ]] && db_args=(--db "$db")
    if err=$(tokenatlas "${db_args[@]+"${db_args[@]}"}" import "$remote_dir/$tag.sqlite3" --label "$tag" 2>&1 >/dev/null); then
        echo "  history: OK"
    else
        printf '  history: ERROR import failed for %s: %s\n' "$tag" "$(terminal_safe "$err")" >&2
        return 1
    fi
}

# Kill a process and everything below it. Under `set -m` a background job is its own process-group leader
# (pgid == pid) on both macOS bash 3.2 and Linux bash 5, so one group kill reaches the whole tree; the
# pgrep -P walk covers children that moved to another group or a shell where job control is unavailable.
kill_tree() {
    local pid="$1" sig="$2" child
    for child in $(pgrep -P "$pid" 2>/dev/null || true); do
        kill_tree "$child" "$sig"
    done
    kill "-$sig" -- "-$pid" 2>/dev/null || true
    kill "-$sig" "$pid" 2>/dev/null || true
}

# Every step still runs after a failure; the host returns 1 if any step had a real failure.
sync_host() {
    local tag="$1" host="$2" fail=0
    echo "Syncing history from $tag ($host)..."
    sync_history "$tag" "$host" || fail=1
    return "$fail"
}

# Run sync_host in a background subshell with a deadline. Returns 124 on timeout.
# The active worker is tracked in WORKER_PID so a termination signal can clean it up.
WORKER_PID=""
run_with_deadline() {
    local pid deadline waited=0
    # Under `tokenatlas collect` (TOKENATLAS_SINGLE_GROUP=1) every descendant stays in the collector's process group, which it signals.
    [[ -n "${TOKENATLAS_SINGLE_GROUP:-}" ]] || set -m
    ( sync_host "$1" "$2" ) &
    WORKER_PID=$!
    pid=$WORKER_PID
    set +m
    deadline=$HOST_TIMEOUT
    while kill -0 "$pid" 2>/dev/null; do
        if [[ $waited -ge $deadline ]]; then
            kill_tree "$pid" TERM
            sleep 1
            kill_tree "$pid" KILL
            { wait "$pid"; } 2>/dev/null || true
            WORKER_PID=""
            return 124
        fi
        sleep 1
        waited=$((waited + 1))
    done
    local rc=0
    wait "$pid" || rc=$?
    WORKER_PID=""
    return "$rc"
}

# On TERM/INT/HUP: TERM the worker's process group, give it 2 s, KILL it, reap it, exit 128+signal.
on_signal() {
    local code="$1" i
    trap '' TERM INT HUP
    # A signal between `&` and the WORKER_PID assignment finds it empty: fall back to the job table.
    local w="${WORKER_PID:-$(jobs -p 2>/dev/null | head -n 1)}"
    if [[ -n "$w" ]]; then
        kill_tree "$w" TERM
        for i in 1 2 3 4 5 6 7 8 9 10; do
            kill -0 "$w" 2>/dev/null || break
            sleep 0.2
        done
        kill_tree "$w" KILL
        { wait "$w"; } 2>/dev/null || true
        WORKER_PID=""
    fi
    exit "$code"
}
trap 'on_signal 143' TERM
trap 'on_signal 130' INT
trap 'on_signal 129' HUP

overall_rc=0
for entry in "${REMOTE_HOSTS[@]}"; do
    tag="${entry%%:*}"
    host="${entry#*:}"
    if [[ "$entry" != *:* ]] || ! valid_pair "$tag" "$host"; then
        printf "Skipping invalid tag:host entry '%s'\n" "$entry" >&2
        continue
    fi

    rc=0
    run_with_deadline "$tag" "$host" || rc=$?
    if [[ $rc -eq 124 ]]; then
        echo "$host: ERROR (timeout after ${HOST_TIMEOUT}s)" >&2
        overall_rc=1
    elif [[ $rc -ne 0 ]]; then
        echo "$host: ERROR (host sync exit $rc)" >&2
        overall_rc=1
    fi
done

echo "Done."
exit "$overall_rc"
