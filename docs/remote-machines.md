# Other machines (for example a Raspberry Pi)

Every machine keeps its own history database. `meta.machine` holds a random id (`m-...`) and each
observation records the machine that first observed it. Nothing is shared live: you move a snapshot.

## Install on the Pi

Python 3.10 or newer, stdlib only, no dependencies. Debian 12/13 and Raspberry Pi OS mark the system
Python as externally managed (PEP 668), so `pip install --user` fails. Use a venv, pinned to a commit
or tag:

    python3 -m venv ~/.local/share/tokenatlas/venv
    ~/.local/share/tokenatlas/venv/bin/pip install "git+https://github.com/Magnus-Gille/tokenatlas@<commit>"
    ln -sf ~/.local/share/tokenatlas/venv/bin/tokenatlas ~/.local/bin/tokenatlas

`pipx install "git+https://github.com/Magnus-Gille/tokenatlas@<commit>"` works as well, and so does a released
version from PyPI: `~/.local/share/tokenatlas/venv/bin/pip install "tokenatlas==<version>"`.

Refresh on a schedule (cron, `crontab -e`). Cron has a short `PATH`, so use the full path.
`refresh --all` covers every harness installed here and reports the others as `absent` (exit 0):

    */30 * * * * ~/.local/bin/tokenatlas refresh --all

Or one line per harness in use:

    */30 * * * * ~/.local/bin/tokenatlas refresh --harness claude
    */30 * * * * ~/.local/bin/tokenatlas refresh --harness pi
    */30 * * * * ~/.local/bin/tokenatlas refresh --harness codex

`remote_sync.sh` puts `~/.local/bin` on `PATH` locally and for every command it runs over
non-interactive ssh, so a user install is found on both ends. To try the sync without touching your
default database, set `ENERGY_MONITOR_DB=/path/to/test.sqlite3`; the local import then uses
`tokenatlas --db <path> import ...`.

## Bring it home

`remote_sync.sh` does this per host, when `command -v tokenatlas || command -v energy-monitor` succeeds
remotely (with `~/.local/bin` prepended to `PATH`): `tokenatlas snapshot` on the host (`energy-monitor snapshot` on a not-yet-upgraded remote), `scp` to `~/.local/state/tokenatlas/remote/<tag>.sqlite3`
(directory mode 0700), then `tokenatlas import ... --label <tag>` locally. A host without it prints
`history: not installed on <tag>` and the loop continues.

### Timeouts and the collector

A stalled host must not block anything else, so every remote call is bounded:

- `ssh` and `scp` get `-o ConnectTimeout=10 -o ServerAliveInterval=10 -o ServerAliveCountMax=3 -o BatchMode=yes`.
  Override the ssh options with `TOKENATLAS_SSH_OPTS`.
- Each host also has an overall limit, `TOKENATLAS_HOST_TIMEOUT` seconds (default 300). A host over the limit
  is killed with its child processes, reported as `<host>: ERROR (timeout after Ns)`, and the script moves
  on to the next host. It then exits 1, but returns promptly.

Schedule `tokenatlas collect` instead of calling `remote_sync.sh` from cron. The sync script ships inside the
package (the repository-root `remote_sync.sh` is a shim that runs it for checkout users). `collect` takes a kernel
lock (`flock` on `collect.lock` in the state directory; released by the OS on exit or crash, so no stale-lock
logic), refreshes and rebuilds the local report first, then runs the sync, and rebuilds the report once more
after every attempted sync, even a failed or timed-out one, so partial imports show up. The whole sync is limited
by `--sync-timeout` (default 600 s): on expiry its process group gets SIGTERM, 2 seconds later SIGKILL. Hosts
come from `--remote tag:host` (repeatable), `REMOTE_HOSTS_OVERRIDE` or the `remote-hosts` file in the state
directory. A busy lock prints `collect: already running` and exits 0.

    */30 * * * * $HOME/.local/bin/tokenatlas collect --remote pi:myhost >> ~/Library/Logs/tokenatlas/collect.log 2>&1

Analytics commands open the history database read-only and SQLite's 30-second busy timeout gives a refresh
time to wait for a short-lived reader. If a refresh meets a transient SQLite busy/locked error, `collect`
waits and retries before failing the step; a final lock failure is reported as a database error, not as a
command-line usage error.

For launchd see "Keeping the report fresh" in the README.

The sync moves only history snapshots. The older per-machine journal and rollup files (`pi_journal.jsonl` and the
like, pulled with rsync) belonged to the retired checkout-only scripts and are no longer synced.

By hand:

    ssh pi tokenatlas snapshot '~/.local/state/tokenatlas/snapshot.sqlite3'
    scp pi:.local/state/tokenatlas/snapshot.sqlite3 ./pi.sqlite3
    tokenatlas import ./pi.sqlite3 --label pi

`snapshot` writes a consistent 0600 copy with the SQLite backup API, atomically, and refuses the
database itself as target. `import` works on a temporary copy (an older schema is migrated on the copy,
never in your snapshot file) and refuses unknown schema versions.

## Identity and reports

- Observation keys are `(provider, harness, machine-if-synthetic, id)`. Provider request ids merge across
  machines (counters take the maximum, never a sum), so a request seen on two machines counts once.
  Ids that had to be synthesized stay per machine.
- Imported rows keep their original `machine`. When the same request exists on both machines the merged
  row carries the machine of the newer copy.
- Import is idempotent; importing a snapshot of this machine is a no-op with a warning.
- Source files are stored as `<machine>:<path>`, so they never collide with local paths, and `doctor`
  counts missing source files for local paths only.
- Each import is an `imports` row with harness `import` and root `<label>`; `meta.machine_labels`
  maps the source machine id to that label. Reports over the merged database cover all machines; use
  `report --records` to see per-observation `machine` and sources.
