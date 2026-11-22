# claude-account-switcher (`cas`)

Run Claude Code on several Claude accounts with **one shared memory**. `cas` tells you what each
account has left (5-hour window, weekly limit, limit resets, usage credits). When an account hits
its limit, it moves the conversation to the next one. It can also open the 5-hour windows before you
start working, with a tiny Haiku message, so the resets land where they help.

No dependencies: Python 3.9+ standard library. Built for macOS (launchd, Keychain). Checked
against Claude Code 2.1.330.

> An independent tool, not made or endorsed by Anthropic. It relies on Claude Code internals
> that can change in any release (see [Notes](#notes)).

## Setup

```sh
git clone https://github.com/alessandromatera/claude-account-switcher.git
cd claude-account-switcher
./bin/cas init            # links `cas` into ~/.local/bin, installs the status line and /accounts
cas add work              # new profile; a browser login opens: sign in with the *other* account
cas status
```

Your existing `~/.claude` login becomes the profile `default`. To give it another name, run
`cas init --name personal`. If your browser is already signed in to a different Claude account,
open the login URL in a private window.

## Daily use

| Command | What it does |
|---|---|
| `cas status` | What's left on every account, and which one to use next |
| `cas run` | Start Claude Code on the best account (`cas run work` picks one). Other arguments go to `claude` unchanged: `cas run -c`, `cas run work --model opus` |
| `cas handoff [name]` | Resume this directory's last conversation on another account |
| `cas reset [name]` | Use a limit reset (see below) |
| `cas env work` | `eval "$(cas env work)"` makes plain `claude` (or an editor started from that shell) use that account |
| `cas rename old new` | Rename a profile; its login is kept (names may contain dots, e.g. `gmail.com`) |
| `cas share --store …` | Share memory with your other Macs through Dropbox (see below) |
| `cas doctor` / `cas sync` | Check the setup / repair shared links and files |

Plain `claude` keeps working: it runs on the `~/.claude` account, with the status line and
`/accounts`. Picking the account and switching it at the limit only happen through `cas run`,
because a running Claude Code session can't change account.

**Inside Claude Code:**
- The status line shows the current account, what's left and when it resets, plus the next account:
  `work 5h 58% left ↻14:20 · wk 76% · ⟲1 │ next: personal 100%`.
  At the limit it tells you what to do next.
- `/accounts` summarizes all accounts. `! cas status` shows the same table without a model call.

**Picking an account.** `cas` uses the quota that expires soonest first. An account whose 5-hour
window is already open loses whatever it doesn't use when the window resets, so it goes first,
soonest reset first, as long as it has at least `min_headroom`% left. Accounts with no open window
keep their quota until it starts, so they come next, lowest weekly usage first. Accounts above
`switch_threshold` (5-hour) or `weekly_threshold` are skipped.

**At the limit.** When Claude exits because the account hit its limit, `cas run` shows what's left
on every account and offers three choices:

1. continue the same conversation on another account (`claude --resume <id>`, same transcript)
2. use a limit reset on this account, if it has one
3. wait

It recommends switching when another account has room (free). Otherwise it recommends a reset when
the wait is long or the reset expires soon. `cas run -y` switches without asking, but it never uses
a reset on its own.

**Which conversation moves.** `cas run` gives each conversation its own id (`--session-id`), so the
handoff resumes exactly that one, even with other sessions open in the same folder.
`cas handoff` (run after exiting) lists the conversations used in this folder in the last 6 hours
with their first prompt, asks which one to move, and warns if one still seems open elsewhere.
`--session <id>` picks one directly.

## Limit resets

Some accounts get limit resets: grants that clear the 5-hour or weekly limit early. Claude Code
uses them through its `/limit-reset` command. `cas` reads their status and **never uses one by
itself**:

- `cas status` lists them per account in the LIMIT RESETS column. `cas status --live` shows the
  count, the kind (5-hour, weekly, full) and the date they expire.
- `cas reset work` shows what that account has left and which resets it holds. It warns you if the
  account still has room, and suggests an account you could switch to at no cost. After you
  confirm, it opens Claude Code on that account with `/limit-reset`, and Claude Code asks you to
  confirm once more before anything is used.

## Shared memory

Each extra account is a separate `CLAUDE_CONFIG_DIR` (`~/.claude-accounts/profiles/<name>`), so
its login, caches and background agents stay separate. What you build up is shared with `~/.claude`:

| Shared by symlink | Shared by sync | Per account |
|---|---|---|
| `projects/` (transcripts + auto-memory), `skills/`, `agents/`, `commands/`, `plugins/`, `output-styles/`, `hooks/`, `todos/`, `plans/`, `file-history/`, `agent-memory/` | `settings.json`, `CLAUDE.md`, `keybindings.json`, user-scope MCP servers | login, `.claude.json`, sessions/daemon/jobs, prompt history |

Files are synced instead of symlinked because Claude Code refuses to write settings through a
symlink. The sync is three-way: `cas` keeps the last synced copy as a base. When both sides edited
a JSON file it merges key by key; for `CLAUDE.md` the newer copy wins and the other one goes to
`~/.claude-accounts/backups/`. The sync runs every time you `cas run`, and on `cas sync`.

## Sharing memory between machines

```sh
cas share --store ~/Dropbox/<your folder>/.claude-shared    # on each Mac, once
cas share --status
```

Shared through that folder: the user `CLAUDE.md`, the auto-memory of every project under Dropbox,
`agents/`, `commands/` and your skills. Not shared: `skills/synced`, which is Claude Code's own
per-account sync of claude.ai skills.

- **Project memory.** The same Dropbox project has a different path on each Mac
  (`/Users/alice/Dropbox/X` vs `/Users/bob/Dropbox/X`). Claude Code keys memory by path, so
  cas maps both to `memory/<path relative to Dropbox>`. Memory a project has on one Mac is waiting
  on the other before you first open it there. Projects outside Dropbox stay local.
- **When it syncs.** Hooks sync at session start (everything) and after each reply (that
  project's memory), plus on `cas sync`. Changes arrive as fast as Dropbox carries them.
- **How.** Files are copied, not symlinked into Dropbox. The sync is three-way, so deletions
  propagate. When both Macs changed a file: `MEMORY.md` indexes are merged line by line; for other
  files the newer one is kept and the other becomes `<name>.conflict-<host>` (linked from
  `MEMORY.md`). Nothing is dropped. The first `cas share` on each Mac writes a full backup to
  `~/.claude-accounts/backups/`.
- **Not shared.** Logins, conversations (transcripts) and `settings.json`, which hold
  machine-specific paths. Log in separately on each Mac; copying tokens between machines would
  make one log the other out when its token refreshes.

## Opening 5-hour windows ahead of time

A window starts with your first message and lasts 5 hours. Start at 09:00 and you get 09–14 and
14–19. Send one tiny message at 06:00 and you get 06–11, 11–16 and 16–21: an extra window during the
day, with the first reset in mid-morning.

```sh
cas plan --start 09:00 --end 19:00          # preview
cas schedule apply --start 09:00 --end 19:00 --days mon-fri
cas schedule show | cas schedule remove
cas ping work                               # open a window now
```

```
          06  08  10  12  14  16  18  20
                ▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔        work hours
personal  ██████████▓▓▓▓▓▓▓▓▓▓███████████·  pings 06:00 11:02 16:04
work      ·····██████████▓▓▓▓▓▓▓▓▓▓▓······  pings 08:30 13:32
```

- `stagger` (default) offsets each account by 5h/N, so some account resets every 5h/N hours. This
  suits continuous heavy use. `align` puts every account on the same chain.
- The ping is `claude -p` on `prime_model` (default `haiku`), with no tools, no MCP and no saved
  session. It is skipped when a window is already open, or when the Mac woke up after work hours.
- `cas run` also opens, in the background, the window of every *other* account that has none
  open (`prime_others_on_start`). With sustained use this doesn't add quota (two accounts give at
  most two windows every 5 hours), but if you switch later, that account's reset comes sooner and
  more of its windows fall inside your working hours. It costs one tiny message per account.
- launchd runs a missed ping when the Mac wakes up. To wake it on time, `cas schedule apply`
  prints the `sudo pmset repeat wakeorpoweron …` line (the Mac must be plugged in).

## Where usage numbers come from

Freshest source wins, and every number shows its source and age:

1. **status line**: Claude Code hands the current account's `rate_limits` to the status line on
   every refresh; `cas statusline` records them.
2. **Claude Code's cache**: `cachedUsageUtilization` and `unusedLimitResetStatusCache` in each
   profile's `.claude.json`.
3. **live**: `GET https://api.anthropic.com/api/oauth/usage?cedar_ember=1`, the endpoint `/usage`
   uses, called with that profile's own OAuth token. The token is read from the Keychain at call
   time, sent only to api.anthropic.com, and never printed or stored. It runs when cached data is
   older than `live_max_age_s`; set `live_usage` to `never` to turn it off. Expired tokens are not
   refreshed by `cas`; Claude Code refreshes them the next time it runs on that account.

A window whose reset time has passed counts as 0% used, so an account idle for 5+ hours shows as
fresh. One limit: usage on claude.ai or the desktop app shares the same quota, and only appears once
one of these sources refreshes.

## Configuration

`~/.claude-accounts/config.json` → `settings`:

| Key | Default | Meaning |
|---|---|---|
| `switch_threshold` | 90 | 5-hour % at which an account counts as exhausted |
| `weekly_threshold` | 97 | same, weekly |
| `min_headroom` | 15 | an open window is preferred only with this much left |
| `handoff` | `ask` | `ask`, `auto` (switch without asking; never uses resets) or `off` |
| `live_usage` | `auto` | `auto`, `always` or `never` |
| `prime_model` | `haiku` | model for window-opening pings |
| `prime_others_on_start` | true | `cas run` opens the other accounts' windows in the background |
| `shared_store` | none | folder shared with your other Macs (`cas share`) |
| `dropbox_root` | `~/Dropbox` | projects under it share memory across Macs |
| `sync_mcp` | true | copy user-scope MCP servers from `~/.claude.json` into profiles |
| `schedule` | 09:00–19:00 mon-fri stagger | written by `cas schedule apply` |

Per profile: `cas set work --label "Work Max" --model sonnet --disable`.

## Notes

- `cas` relies on Claude Code internals that are not a public API: the Keychain naming per
  `CLAUDE_CONFIG_DIR`, the usage endpoint, the status line's `rate_limits` and `/limit-reset`. It was
  checked against 2.1.330. If an update changes one of them, `cas` falls back to the remaining
  sources, and `cas doctor` shows what broke.
- Each account logs in normally from this machine; `cas` doesn't hide or spoof anything. Check that
  the way you use several subscriptions fits Anthropic's terms for your plans.
- `cas uninstall` removes the status line, `/accounts`, the schedules and the `cas` link. Profiles
  stay; `cas remove <name> --purge` logs one out and deletes it.

## Development

```sh
python3 -m unittest discover -s tests -t .
```

Tests run in a throwaway `HOME`. Override locations with `CAS_HOME`, `CAS_HUB_DIR` and `CAS_HUB_JSON`.

## License

MIT, see [LICENSE](LICENSE).
