English | [中文](README.zh-CN.md)

# LocalhostMaster

A lightweight Windows TUI for inspecting local listening ports and the
processes behind them. Built on **Python 3**, **psutil** (native Windows
IP Helper access) and **prompt_toolkit**.

```
┌ LocalhostMaster 0.1.0 ── endpoints: 12/136 | sort: port filter: all | docker: ok ┐
│  PROTO  ADDRESS                 PORT     PID  PROCESS              CATEGORY     OPEN │
│  TCP    127.0.0.1               8081   28200  llama-server.exe     llama.cpp    yes  │
│  TCP    127.0.0.1               8082   21664  llama-server.exe     llama.cpp    yes  │
│  TCP    ::                      3002   30812  com.docker.backend   docker       yes  │
│  UDP    127.0.0.1              14022   15896  rustrover            dev-server   -    │
├──────────────────────────────────────────────────────────────────────────────────────┤
│ [Enter] arm   [r] refresh   [/] search   [f] filter   [C] categories   [?] help      │
└──────────────────────────────────────────────────────────────────────────────────────┘
```

## Features

- TCP **LISTEN** and UDP **bound** endpoints, IPv4 and IPv6.
- Optional connected TCP rows (ESTABLISHED / TIME_WAIT / …) with their remote
  endpoint, shown only with `--show-established` / `a`.
- Process name, PID, executable-derived metadata and category per endpoint.
- Double-Enter to open a TCP **listener** in the **system default browser**.
- A category system (built-in + user-defined) with colours, icons and rules.
- Background refresh that never blocks input; Docker enrichment is optional.
- Non-interactive plain-text / JSON output for scripting.

## Requirements

- Windows 11 (first release targets Windows only; small platform seams exist)
- Python 3.11+ (**currently verified on Python 3.14.6**; 3.11 is not installed on
  the development machine and has not been exercised)
- `psutil>=5.9`, `prompt_toolkit>=3.0`

## Run

From the project root:

```powershell
$env:PYTHONPATH = "src"
python -m localhostmaster            # full-screen TUI
python -m localhostmaster --once     # one scan, plain text, exit
python -m localhostmaster --json     # one scan, JSON, exit
python -m localhostmaster --version
```

Installing the package exposes the `localhostmaster` console script. The local
install path has been verified in a throwaway virtual environment:

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install . --no-deps
.venv\Scripts\localhostmaster --version     # localhostmaster 0.1.0
```

> Offline: the build backend needs `setuptools`; pre-install it and pass
> `--no-build-isolation` to complete the local install without network access.

### CLI options

| Option | Meaning |
| --- | --- |
| `--version` | print version and exit |
| `--once` | one scan, plain-text table, exit |
| `--json` | one scan, JSON, exit |
| `--show-established` | include non-listening TCP connections (ESTABLISHED, TIME_WAIT, …) and their remote endpoint |
| `--no-docker` | disable Docker enrichment |
| `--config PATH` | use an alternate `config.toml` |
| `--refresh-ms N` | override the auto-refresh interval (ms, minimum 250) |
| `--no-color` | disable colours |
| `--debug` | write rotating logs to `%LOCALAPPDATA%\LocalhostMaster\logs` |

When stdout is not a TTY (redirected/piped), the program prints a plain-text
table and exits instead of entering the full-screen UI.

## Key bindings

| Key | Action |
| --- | --- |
| `Up`/`Down` | move cursor |
| `PageUp`/`PageDown` | page |
| `Home`/`End` | first / last |
| `Enter` | arm; press again within 2.5s to open in browser |
| `k` | arm force-stop; press again within 2.5s to kill the process |
| `Esc` | cancel confirmation / close dialog |
| `r` | refresh now |
| `Space` | pause / resume auto refresh |
| `/` | search (address, port, process, category) |
| `f` | filter (all / container / openable / category) |
| `s` | cycle sort (port / process / category / pid) |
| `a` | toggle established TCP connections |
| `c` | copy the selected TCP URL to the clipboard |
| `C` | category manager |
| `?` | help |
| `q`, `Ctrl-C` | quit |

`j`/`k` are still available as local navigation inside the filter, category and
icon-picker lists, but the main port list uses `Up`/`Down` only, so `k` is
unambiguous there.

### Double-Enter

1. The first `Enter` only **arms** the selected endpoint; the status bar shows
   `Press Enter again within 2.5s to open …`.
2. A second `Enter` between 150 ms and 2500 ms **opens** it in the default
   browser. Presses within the first 150 ms (keyboard auto-repeat) are ignored.
3. Moving the cursor, searching, filtering, sorting, switching scheme or
   pressing `Esc` cancels the armed state.

Only TCP **LISTEN** endpoints can be opened. Connected TCP rows (ESTABLISHED,
TIME_WAIT, CLOSE_WAIT, …) and UDP rows show `-` in the OPEN column, never arm,
and `Enter` only explains why they cannot be opened. The table adapts to the
terminal width: on very narrow terminals the OPEN (and other) columns are
omitted, while medium and wide terminals show OPEN.

### Double-`k` force-stop

The first `k` only **arms** the selected endpoint; the status bar shows
`Press k again within 2.5s to force-stop …`. A second `k` between 150 ms and
2500 ms submits the termination. Presses within the first 150 ms (keyboard
auto-repeat) are ignored, and a press after 2.5 s starts a new confirmation.

Safety rules:

- It stops the **whole host process** behind the selected row, not just one
  socket. Any *other* ports that process holds disappear with it, and the status
  bar warns you how many other visible endpoints that affects.
- It never kills a process tree, never terminates child processes and never
  escalates privileges. Only `psutil.Process(pid).kill()` is used — no shell,
  PowerShell, `taskkill` or `Stop-Process`.
- Only **TCP LISTEN** and **UDP BOUND** rows can be force-stopped. Connected TCP
  rows (ESTABLISHED, TIME_WAIT, …) and rows without a PID are refused.
- The process identity (`PID` + creation time + endpoint) is re-verified right
  before the kill, so a recycled PID is never hit; the selected endpoint is also
  re-scanned to confirm it still belongs to that PID and is still listening.
- LocalhostMaster itself, PID 0, PID 4, Windows core/service processes and
  Docker host proxies (`com.docker.backend`, `wslrelay`, `dockerd`, `docker`)
  are always refused.
- When `create_time` cannot be read, the target is refused rather than killed
  unverified.
- Termination runs on a background thread, so the UI never blocks, and the port
  list is refreshed as soon as it finishes.
- The result distinguishes **exited** from **request sent but exit not
  confirmed** (`Force-stop was requested, but exit was not confirmed.`). An
  `AccessDenied` result is a normal permission limit, not a bug.

## URL rules

| Bind address | Browser host |
| --- | --- |
| `0.0.0.0`, empty | `127.0.0.1` |
| `::`, empty | `[::1]` |
| `127.0.0.1` | `127.0.0.1` |
| `::1` | `[::1]` |
| other concrete address | used as-is (IPv6 bracketed) |

Scheme defaults to `http`; a category may set `https`. Only `http` and `https`
are accepted — any other value is ignored with a warning and falls back to
`http`, so an arbitrary scheme can never reach the OS URL handler. IPv6 hosts
are always bracketed. No HTTP probing is performed.

## Configuration

Configuration lives in `%APPDATA%\LocalhostMaster\`.

- `config.toml` – general settings (user maintained)
- `categories.toml` – user categories (managed by the TUI, editable by hand)

Neither file is created until you first save something, so the application runs
with built-in defaults out of the box.

### `config.toml`

```toml
[general]
refresh_ms = 3000
show_established = false
color_mode = "auto"      # auto | truecolor | 256 | 16 | none
docker_enabled = true
docker_ttl_s = 15
docker_timeout_s = 2
double_enter_ms = 2500
arm_guard_ms = 150
```

Precedence: CLI flags > user config > built-in defaults.

`refresh_ms` is clamped to a minimum of 250 ms (with a warning) so a bad config
cannot create a busy loop; `--refresh-ms` below 250 is rejected with an error.
`docker_timeout_s` and `docker_ttl_s` are likewise clamped to positive values.
`arm_guard_ms` is clamped to at most half of `double_enter_ms` (with a warning),
otherwise the auto-repeat guard would swallow every second press and
double-Enter / double-`k` could never fire. An unknown `color_mode` falls back to
`auto` with a warning.

### `categories.toml`

```toml
# LocalhostMaster user categories.
# This file may be rewritten by the application; comments are not preserved.

[[category]]
id = "user.my-llama"
name = "my-llama"
color = "#7C3AED"
icon = "◆"
priority = 120
open_in_browser = true
scheme = "http"
process_globs = ["llama-server*", "llama-cli*"]

[[category]]
id = "user.grafana"
name = "grafana"
color = "#F59E0B"
priority = 80
ports = [3000]
port_ranges = ["3001-3010"]
protocols = ["TCP"]
```

`icon` is chosen from a small preset list in the UI (see **Category icons**
below) and stored as the literal glyph. The file format is unchanged, so
hand-written `icon` values keep working.

Matching rules:

- **User rules always win over built-in rules.**
- Within a source: higher `priority` first, then file order; first match wins.
- Different fields combine with **AND**; multiple patterns inside one field
  combine with **OR**.
- Any `exclude_*` match vetoes the rule.
- `process_globs`, `executable_globs` and `command_line_globs` use globbing and
  are case-insensitive on Windows. A pattern prefixed with `re:` is a regex
  (Python `re`); invalid regexes are skipped with a warning.
- A rule that requires the command line never matches when it is unreadable
  (see permissions below).

Writes are atomic (temp file + `os.replace`), and a corrupt file is never
overwritten automatically.

## Built-in categories

| Category | Colour | Matches |
| --- | --- | --- |
| `llama.cpp` | purple | `llama-server*`, `llama-cli*`, `llamafile*` |
| `docker` | blue | `com.docker.backend*`, `wslrelay*`, `dockerd*`, `docker*`, or any known container mapping |
| `database` | orange | `postgres*`, `mysqld*`, `mariadbd*`, `mongod*`, `redis-server*` |
| `dev-server` | green | `node*`, `deno*`, `bun*`, `python*`, `uvicorn*`, `vite*`, `webpack*`, `next*` |
| `system` | grey | `svchost*`, `services*`, `wininit*`, `lsass*`, `System` |
| `unknown` | grey | fallback |

Categories are always shown as **text**; colour is never the only signal. In
`none`/`NO_COLOR`/`--no-color` mode, Unicode icons fall back to ASCII.

## Category icons

The category form no longer accepts free-text icon input. The **Icon** field is
a read-only selector: press `Enter` or `Space` to open a picker that lists every
preset with its actual glyph and a readable name (for example `◆  Diamond`).
`Up`/`Down` (or `j`/`k`) move, `Enter`/`Space` choose, and `Esc` returns to the
form without changing anything.

- New and edited categories must use a preset icon; the default is `◆ Diamond`.
- Presets are single-width symbols that render reliably in Windows Terminal:
  `◆`, `▣`, `▤`, `▸`, `•`, `·`, `●`, `■`, `▲`, `★`, `◇`, `□`, `○`, `◉`, `⬢`, `✦`.
- The chosen glyph is only written to `categories.toml` when you **save** the
  form; cancelling changes nothing.
- Existing hand-written icons keep working. An icon that is not in the preset
  list shows as `Legacy/custom` in the form and is preserved unless you actively
  pick a preset; in ASCII-only mode it falls back to `*`. Unknown glyphs never
  break loading.

The **Color** field must be a prompt_toolkit colour name (for example `red` or
`ansiblue`) or `#RGB`/`#RRGGBB`. An invalid value is rejected by the form; a bad
colour already on disk never prevents the UI from starting (it falls back to the
default grey).

## Permissions

- Works as a normal user; no elevation is required.
- The socket table, PID and process name are visible for all endpoints.
- **Command lines of processes owned by other users (and protected system
  processes) are not readable without administrator rights.** When a rule
  depends on the command line and it is unavailable, that rule does not match.

## Docker

Docker enrichment is optional and never on the critical path:

- Runs `docker ps --format "{{json .}}"` on a background thread, at most once
  per `docker_ttl_s` (default 15 s), with a `docker_timeout_s` (default 2 s)
  timeout and no console window.
- Missing/stopped/slow Docker degrades silently; the main list is unaffected.
- Container identity is used only to label entries that the OS socket table
  already attributes to Docker's proxy processes (`com.docker.backend`,
  `wslrelay`). This is a **best-effort inference**, worded as "published by
  container X", and is not a claim that the process owns the socket.

## Testing

```powershell
$env:PYTHONPATH = "src"
python -m unittest discover -s tests -v
python -m compileall src
```

Tests use dependency injection and fakes: they never open a real browser and
only bind temporary loopback sockets on random ports. Force-stop is tested with
an injected fake terminator, so **no automated test ever terminates a real
process**.

## Performance

Measured on the development machine (Windows 11, 20 logical cores, ~127
endpoints / 42 PIDs):

| Metric | Value |
| --- | --- |
| Startup to `--once` (incl. interpreter) | ~0.78 s |
| Socket scan only | ~6 ms |
| First snapshot (cold process cache) | ~0.19 s |
| Steady-state snapshot (warm cache) | ~2.6 ms build (plus ~6 ms socket scan) |
| PID re-verification round (every ~45 s) | ~11 ms |
| Full process re-resolve of every PID | ~200 ms (deliberately avoided) |
| Idle CPU at 1 s refresh | ~0.5% of one core |
| Idle RSS | ~28 MB |
| Docker `ps` refresh | ~0.27 s (background, cached) |
| External processes per steady-state scan | 0 |

The full scan is always performed and the resulting snapshot replaces the
previous one; there is no incremental scanning.

## Known limitations

- Windows-only for now; the browser opener and clipboard are Windows-specific.
- Non-admin users cannot read other users' command lines, so command-line rules
  may not match system/other-user processes.
- Docker published-port mapping is an inference from `docker ps`, not the OS
  socket table.
- Any TCP *listener* can be opened in a browser even if it is not HTTP;
  LocalhostMaster never probes the protocol, so opening a non-HTTP port may
  show an error page.
- Connected TCP rows and UDP endpoints cannot be opened.
- PID identity is re-verified every ~45 s (and whenever a PID's endpoint set
  changes); a process killed and recycled with an identical PID between two
  verifications could still briefly show stale metadata.
- Force-stop cannot be atomic: the endpoint and identity are re-checked
  immediately before the kill, which shrinks — but cannot fully eliminate — the
  window between verification and `TerminateProcess`.
- Docker host proxies are deliberately refused, so a container's published port
  cannot be stopped from LocalhostMaster (use `docker stop` for that).
- A standalone `.exe` is not produced by default (no bundler is installed or
  required).

## Project layout

```
src/localhostmaster/
  cli.py                 argument parsing and non-interactive output
  scanner.py             psutil scan + address normalization
  process_resolver.py    PID -> process metadata (performance-aware cache)
  classifier.py          built-in + user rule matching
  config.py              config.toml loading/validation
  category_store.py      categories.toml (atomic writes, minimal TOML writer)
  docker_resolver.py     optional, background Docker enrichment
  url_builder.py         pure URL construction
  browser.py             system default browser opener (injectable)
  clipboard.py           Win32 Unicode clipboard
  refresh.py             snapshot builder + background refresh worker
  state.py               double-Enter state machine, sort/filter/selection
  kill_state.py          double-k force-stop state machine
  process_terminator.py  safe psutil kill boundary (policy + re-verification)
  icons.py               central preset icon registry
  tui/                   prompt_toolkit application, controls, styles
tests/                   unittest suite (fakes, no real browser/terminal/kill)
```

## License

MIT
