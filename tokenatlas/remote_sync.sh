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
#
# Back-off (TOKEN-198): a host that fails TOKENATLAS_BACKOFF_AFTER runs in a row (default 3; 0 disables) is skipped, not
# retried on every cron run: for TOKENATLAS_BACKOFF_BASE seconds (default 3600), doubling with each further failure up to
# TOKENATLAS_BACKOFF_MAX (default 43200). A skip prints "history: SKIPPED ..." and is not a failure. TOKENATLAS_FORCE_SYNC=1
# ignores the back-off. The per-host state lives in <state>/remote/<tag>.sync-state (read by `tokenatlas doctor` and the report).

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

for v in TOKENATLAS_BACKOFF_AFTER:3 TOKENATLAS_BACKOFF_BASE:3600 TOKENATLAS_BACKOFF_MAX:43200; do
    name="${v%%:*}"; value="${!name:-${v#*:}}"
    if ! [[ "$value" =~ ^[0-9]+$ ]]; then printf "%s must be a non-negative integer, got '%s'\n" "$name" "$value" >&2; exit 2; fi
    printf -v "$name" '%s' "$((10#$value))"  # 10#: a leading zero is not octal
done

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

# Per-host sync state: fails (consecutive), last_ok / last_try / next_try (epoch seconds). Best effort: a state file that cannot
# be written or read only disables the back-off, it never fails a sync.
STATE_DIR="$HOME/.local/state/tokenatlas/remote"
state_get() { # tag key -> value (empty when unknown)
    { sed -n "s/^$2=\([0-9]*\)\$/\1/p" "$STATE_DIR/$1.sync-state" 2>/dev/null | head -n 1; } || true
}
state_put() { # tag fails last_ok last_try next_try
    { mkdir -p "$STATE_DIR" && chmod 700 "$STATE_DIR" &&
      printf 'fails=%s\nlast_ok=%s\nlast_try=%s\nnext_try=%s\n' "$2" "$3" "$4" "$5" > "$STATE_DIR/$1.sync-state"; } 2>/dev/null || true  # four short lines: one write
}
fmt_ts() { # epoch -> UTC text (GNU or BSD date), else the epoch itself
    [[ -n "$1" ]] || { printf 'never'; return; }
    date -u -d "@$1" '+%Y-%m-%d %H:%M UTC' 2>/dev/null || date -u -r "$1" '+%Y-%m-%d %H:%M UTC' 2>/dev/null || printf '%s' "$1"
}
record_result() { # tag rc
    local tag="$1" rc="$2" now fails last_ok wait
    now=$(date +%s)
    last_ok=$(state_get "$tag" last_ok)
    if [[ "$rc" -eq 0 ]]; then
        state_put "$tag" 0 "$now" "$now" 0
        return
    fi
    fails=$(( $(state_get "$tag" fails) + 1 ))
    wait=0
    if [[ "$TOKENATLAS_BACKOFF_AFTER" -gt 0 && $fails -ge $TOKENATLAS_BACKOFF_AFTER ]]; then
        wait=$TOKENATLAS_BACKOFF_BASE
        local i=$((fails - TOKENATLAS_BACKOFF_AFTER))
        while [[ $i -gt 0 && $wait -lt $TOKENATLAS_BACKOFF_MAX ]]; do wait=$((wait * 2)); i=$((i - 1)); done
        [[ $wait -gt $TOKENATLAS_BACKOFF_MAX ]] && wait=$TOKENATLAS_BACKOFF_MAX
    fi
    state_put "$tag" "$fails" "${last_ok:-0}" "$now" "$((now + wait))"
}
# Returns 0 (after printing why) when the host is inside its back-off window.
backing_off() {
    local tag="$1" next now
    [[ -z "${TOKENATLAS_FORCE_SYNC:-}" && "$TOKENATLAS_BACKOFF_AFTER" -gt 0 ]] || return 1
    next=$(state_get "$tag" next_try); now=$(date +%s)
    [[ -n "$next" && "$next" -gt "$now" ]] || return 1
    # A deadline further away than the longest back-off (clock jump, or a lowered TOKENATLAS_BACKOFF_MAX) is not trusted.
    [[ $((next - now)) -le "$TOKENATLAS_BACKOFF_MAX" ]] || return 1
    printf '  history: SKIPPED %s: %s failed syncs in a row, next try after %s (last success %s); TOKENATLAS_FORCE_SYNC=1 tries now\n' \
        "$tag" "$(state_get "$tag" fails)" "$(fmt_ts "$next")" "$(fmt_ts "$(state_get "$tag" last_ok)")"
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
    # stderr is kept only to recognise Tailscale SSH check mode; it is never logged (it may carry a login URL).
    local probe_rc=0 probe_err
    probe_err=$(ssh "${SSH_OPTS[@]}" -- "$host" 'PATH="$HOME/.local/bin:$PATH"; command -v tokenatlas || command -v energy-monitor' 2>&1 >/dev/null) || probe_rc=$?
    if [[ $probe_rc -eq 1 ]]; then
        echo "  history: not installed on $tag"
        return 0
    elif [[ $probe_rc -ne 0 && "$probe_err" == *"Tailscale SSH requires an additional check"* ]]; then
        echo "  history: ERROR $tag: Tailscale SSH check needs re-authentication (run ssh $host interactively)" >&2
        return 1
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

    if backing_off "$tag"; then continue; fi
    rc=0
    run_with_deadline "$tag" "$host" || rc=$?
    record_result "$tag" "$rc"
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
