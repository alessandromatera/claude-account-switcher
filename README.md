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
./bin/cas init            # links `cas` into ~/.local/bin, installs the status line, /accounts and the
                          # `claude` shell function (fresh sessions start on the current account)
cas add work              # new profile; a browser login opens: sign in with the *other* account
cas status
```

**Run it from an installed copy** (recommended when the source folder is synced or you edit it):
`cas install` copies the program to `~/.local/share/claude-account-switcher` and points the `cas`
command, the status line, `/accounts` and scheduled pings at that copy. Edits to the source folder
then change nothing until you run `cas install` again, on each machine; run from the installed copy,
it installs again from the folder it came from. `cas doctor` says when the source has changed since.

Your existing `~/.claude` login becomes the profile `default`. To give it another name, run
`cas init --name personal`. If your browser is already signed in to a different Claude account,
open the login URL in a private window.

## Daily use

| Command | What it does |
|---|---|
| `cas status` | What's left on every account, and which one to use next |
| `cas run` | Start Claude Code on the best account (`cas run work` picks one). Other arguments go to `claude` unchanged: `cas run -c`, `cas run work --model opus` |
| `cas handoff [name]` | Resume this directory's last conversation on another account |
| `cas send` / `cas receive` | Move a conversation to another machine: `send` where you leave it, `receive` where you continue it (needs `cas share`, see [below](#moving-a-conversation-to-another-machine)) |
| `cas use work` | Make `work` the current account: a fresh `claude` starts there, on all your machines with `cas share` |
| `cas reset [name]` | Use a limit reset (see below) |
| `cas env work` | `eval "$(cas env work)"` makes plain `claude` (or an editor started from that shell) use that account; `cas env` alone gives the current account |
| `cas rename old new` | Rename a profile; its login is kept (names may contain dots, e.g. `gmail.com`) |
| `cas share --store …` | Share memory with your other machines through a synced folder (see below) |
| `cas doctor` / `cas sync` | Check the setup / repair shared links, files and stale logins |
| `cas install` | Run cas from a copy outside the source folder; run it again to update (see Setup) |

**Plain `claude`** starts on the current account: the one cas last started or handed off to
(`cas status` names it), on any of your machines when `cas share` is on, since limits belong to
the account and not the machine. For that, `cas init` adds a small `claude` shell function to `~/.zshrc`
(and `~/.bashrc` if you have one), which runs `cas claude`; `cas init --no-shell` leaves plain
`claude` on `~/.claude`. It prepares the profile like `cas run` does, but it doesn't pick the best
account or offer a handoff at the limit: that needs `cas run`, because a running Claude Code
session can't change account. Exceptions:
- Setup commands (`claude mcp`, `claude plugin`, `claude update`, `claude install`, `claude doctor`)
  keep running on `~/.claude`, which cas spreads to every profile.
- An account you already chose is kept: a shell with `cas env` applied, or a `claude` started inside
  a session.

The function works in terminals opened after `cas init`; in one that was already open, `claude`
still runs on `~/.claude` until you `source ~/.zshrc`. When that puts a session on another account
than cas's, its status line says so and gives the fix:
`⚠ opened outside cas, which is on work → exit, then: source ~/.zshrc && claude -c`
(`claude -c` reopens the same conversation, now on `work`).

**Inside Claude Code:**
- The status line shows the current account and the next one, each with what's left of its 5 hours
  and its week and when each resets:
  `work 5h 58% left ↻14:20 · wk 76% ↻Fri 10:00 · ⟲1 │ next: personal 5h 100% · wk 41% ↻Sat 09:00`.
  A 5-hour window not opened yet has no reset time: it starts with use.
  When no other account has room, it shows the one that frees up first, with `⟲` if a limit reset
  could free it now: `│ personal 5h 0% ↻15:10 · wk 41% ↻Sat 09:00 · ⟲`. It shows that too once this
  account is at its limit. A figure older than twice `live_max_age_s` says how old it is, e.g. `(11h old)`.
  At the limit it tells you what to do next, with what's left on the account to switch to:
  `│ limit: exit, then cas handoff → personal 5h 100% · wk 41% ↻Sat 09:00`. Near it, while this
  conversation's agents are at work, it says so:
  `⚠ 2 agents running, limit near → let them finish, then exit and cas handoff → personal 5h 100% · wk 41% ↻Sat 09:00`
  (see [Agents](#agents)).
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
3. open Claude Code anyway on the account that frees up first, and wait there (when no account has
   room): you can still read the conversation and queue your next prompt, and Claude Code shows its
   own reset time. `cas handoff` and `cas receive` offer it too
4. stop

It recommends switching when another account has room (free). Otherwise it recommends a reset when
the wait is long or the reset expires soon, and opening Claude Code and waiting when it isn't. `cas run -y` switches without asking, but it never uses
a reset on its own.

**Which conversation moves.** `cas run` gives each conversation its own id (`--session-id`), so the
handoff resumes exactly that one, even with other sessions open in the same folder.
`cas handoff` (run after exiting) lists the conversations used in this folder in the last 6 hours
with their first prompt, asks which one to move, and warns if one still seems open elsewhere.
`--session <id>` picks one directly.

### Agents

Subagents run inside the Claude Code session, on its account, so they stop when it does: when you
exit, and also when the account hits its limit (each of their requests counts against it). A running
session can't change account, so no handoff can carry a running agent over. What it does instead:

- **Continue them.** Each agent's transcript stays on disk next to the conversation's, and in a
  resumed session Claude can continue an agent by sending it a message (`SendMessage` to its id); it
  picks up with its context, now on the new account. When a handoff (`cas run` at the limit, or
  `cas handoff`) resumes a conversation whose last session stopped agents before they finished,
  cas opens the new session with a message that asks Claude to do that for each one:
  `· 2 agents stopped before finishing; Claude continues them on personal: survey, tests`.
  Agents stopped more than an hour before the conversation's last message are left alone (the
  conversation went on without them), and so are agents' own agents. `cas handoff --no-agents`
  skips it once, `resume_agents: false` always. A handoff that opens `/limit-reset` can't also send
  that message; cas prints which agents to ask Claude to continue afterwards.
- **Warn first.** From `agent_warning` points before `switch_threshold` (default 15, so from 75%
  of the 5-hour window used), the status line warns while agents are at work. Switching once they
  are done interrupts nothing. At the limit it counts the ones that stopped:
  `limit: exit, then cas handoff → personal 5h 100% · wk 41% ↻Sat 09:00, which continues the 2 agents`.

## Logins over SSH (macOS Keychain)

Claude Code keeps each account's login in the macOS login Keychain, or in
`<config dir>/.credentials.json` when it can't write to the Keychain. Over SSH (and in a tmux
started from SSH) the Keychain is often locked, and a locked Keychain can't be read without a
password prompt that SSH can't show. Without cas stepping in, an account looks logged out after a while:

- A login saved while the Keychain was unlocked can't be read once it locks. A new session says
  "Not logged in". One already running stops at its next token renewal (within 8 hours), because
  Claude Code won't move the login out of the Keychain.
- A login made while the Keychain is locked goes to `.credentials.json`, but the Keychain keeps
  the old copy. Once the Keychain unlocks, Claude Code reads that stale copy first.

So before Claude Code starts (`cas run`, plain `claude`, `cas handoff`, pings), cas keeps one
login per account:
- **Stale copy.** When `.credentials.json` holds a newer login than the Keychain, cas deletes the
  Keychain copy. This works while the Keychain is locked. cas compares when each copy was
  written, so it never reads the secret.
- **Login only in the locked Keychain.** cas offers to unlock it with `security unlock-keychain`
  (macOS asks for your Mac password; cas never sees it). If the Keychain locks itself again
  (idle timeout or sleep), cas says so and offers to stop that, because a session still open
  then can't renew its login.
- **Inside a session.** If the login of a running session ends up in the locked Keychain, the status
  line warns while there is still time: `⚠ Keychain locked: this login can't renew → security
  unlock-keychain` (run it in any terminal on that Mac).

Scheduled pings don't start Claude Code on a login they can't use; they log why instead.
`cas doctor` shows where each login is stored.

## Limit resets

Some accounts get limit resets: grants that clear the 5-hour or weekly limit early (a full reset
clears both). claude.ai lists them under Settings → Usage; Claude Code can use them through its
`/limit-reset` command when that command is enabled for the account. `cas` **never uses one by
itself**.

- **Seeing them.** The usage endpoint doesn't share reset details with cas, so they usually show
  as `?`. Record what claude.ai shows:
  ```sh
  cas reset <profile> --set 4 --kind full --expires "2026-12-10 23:00"
  ```
  From then on `cas status`, the status line and the limit menu show them. When an account's usage
  drops sharply before its window resets, a reset was used (through cas, Claude Code or
  claude.ai), and the count goes down by one.
- **Using them.** `cas reset <profile>` shows what that account has left: the 5-hour window, the
  weekly limit (a full or weekly reset also clears that) and the resets it holds. It also
  suggests an account you could switch to at no cost. After you confirm, it opens Claude Code's
  `/limit-reset` on that account, or, when Claude Code can't use resets there, claude.ai's usage
  page, where you click "Reset for free" yourself.
- **On several machines.** With `cas share` on, the count you record and the resets each machine
  sees being used travel through the shared folder. Each machine writes only its own file there.
  When two machines saw the same reset, their observation times overlap and it counts once.

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

`cas` doesn't move anything between machines itself. You give it a folder your machines already
keep in sync (any sync client, or a share mounted on each of them), and it copies memory in and
out of that folder.

```sh
cas share --store <a folder your machines keep in sync>    # on each machine, once
cas share --status
cas share --off
```

Shared through that folder: the user `CLAUDE.md`, the auto-memory of your projects, `agents/`,
`commands/` and your skills. Not shared: `skills/synced`, which is Claude Code's own per-account
sync of claude.ai skills. cas also shares its own records there: the limit resets you have, and
the current account (when `cas run` or a handoff moves to another account on one machine, a fresh
`claude` on the other starts there too; it's matched by account, so profile names can differ).

- **Project memory.** Claude Code keys memory by the project's full path, and that path differs
  between machines (`/Users/alice/code/X` vs `/Users/bob/code/X`). cas maps both to
  `memory/<path relative to projects_root>`. By default `projects_root` is your home, so any
  project at the same place under home on both machines shares its memory. The projects
  themselves don't have to be synced. Use `--projects-root` to pick another folder. Projects
  outside it stay local. Memory a project has on one machine is waiting on the other before you
  first open it there.
- **When it syncs.** Hooks sync at session start (everything) and after each reply (that
  project's memory), plus on `cas sync`. Changes reach the other machine as fast as your folder
  syncs; a session that's already open sees them at its next start.
- **How.** Files are copied, not symlinked into the shared folder. The sync is three-way (this
  machine, the shared folder, and the version from the last sync), so deletions propagate. When
  both machines changed a file: `MEMORY.md` indexes are merged line by line; for other files the
  newer one is kept and the other becomes `<name>.conflict-<host>` (linked from `MEMORY.md`).
  Nothing is dropped.
- **Safety.**
  - The first `cas share` on each machine writes a full backup to `~/.claude-accounts/backups/`.
  - If the shared folder changes, is emptied or is replaced, cas starts a fresh sync and re-shares
    this machine's files instead of reading their absence as deletions.
  - If most of a memory folder vanishes at once, cas doesn't delete those files here; it logs a
    warning.
  - Files a cloud client hasn't downloaded yet (`.icloud` placeholders) are skipped until they arrive.
- **Not shared.** Logins, conversations (transcripts) and `settings.json`, which hold
  machine-specific paths. Log in separately on each machine; copying tokens between machines would
  make one log the other out when its token refreshes. A conversation moves when you ask:
  `cas send` (next section).

## Moving a conversation to another machine

```sh
cas send          # on the machine you're leaving: after exiting Claude Code, or `! cas send` inside it
cas receive       # on the other machine, from any folder: continues it there
```

`cas send` puts one conversation in the shared folder (`<shared folder>/handoffs/`, one
compressed archive): its transcript, its agents' transcripts and tool results, its `/rewind`
checkpoints and its todos. Run inside a session, it sends that session (Claude Code names it in
`CLAUDE_CODE_SESSION_ID`); then exit it, because what happens there afterwards stays on that
machine. From a terminal it picks among this folder's recent conversations like `cas handoff`;
`--session <id>` picks one directly. Running it again replaces what you sent.

`cas receive` on the other machine:
- **Finds the project.** It looks for the project at the same place relative to `projects_root`
  (`~/code/X` on both, whatever the user name), as project memory does, and runs there. A project
  outside `projects_root` needs the same path on both machines; otherwise cas asks to continue it
  in the current folder.
- **Rewrites paths.** The old machine's paths in the conversation (project folder and home) become
  this machine's, so Claude finds the files. File checkpoints keep their content as it was.
- **Resumes it.** The conversation goes where Claude Code looks for it here, keyed by the resolved
  project folder (symlinks followed), and cas runs `claude --resume <id>` on the account `cas run`
  would pick (or `cas receive work`). From then on it behaves like `cas run`: at the limit it
  offers a handoff. Agents the last session stopped are continued, as after a handoff
  ([Agents](#agents)); `--no-agents` skips that.
- **Cleans up.** The archive leaves the shared folder once the conversation is in place. If this
  machine already had a different copy (say you moved it here before), that copy goes to
  `~/.claude-accounts/backups/conversations/`.

It waits until the whole archive has synced (the sender records its checksum), and says so while
it's still arriving. Plain `claude` and `cas run` in a folder a conversation was sent for say so;
`cas status` counts what's waiting. `cas receive --list` shows it all; `--discard` drops one
without resuming it (its own machine still has it).

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
   older than `live_max_age_s`, including in the background each time `cas run` or `claude` starts
   a session, so the status line's `next:` is current; set `live_usage` to `never` to turn it off. Expired tokens are not
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
| `resume_agents` | true | a handoff asks Claude to continue the agents the last session stopped |
| `agent_warning` | 15 | the status line warns about running agents this many points before `switch_threshold` (0 = never) |
| `live_usage` | `auto` | `auto`, `always` or `never` |
| `prime_model` | `haiku` | model for window-opening pings |
| `prime_others_on_start` | true | `cas run` opens the other accounts' windows in the background |
| `shared_store` | none | folder shared with your other Macs (`cas share`) |
| `projects_root` | `~` | projects at the same path relative to it share memory across machines |
| `sync_mcp` | true | copy user-scope MCP servers from `~/.claude.json` into profiles |
| `schedule` | 09:00–19:00 mon-fri stagger | written by `cas schedule apply` |

Per profile: `cas set work --label "Work Max" --model sonnet --disable`.

## Notes

- `cas` relies on Claude Code internals that are not a public API: the Keychain naming per
  `CLAUDE_CONFIG_DIR`, the usage endpoint, the status line's `rate_limits`, `/limit-reset`, and the
  subagent transcripts (`<session>/subagents/agent-<id>.jsonl` and `.meta.json`), and, for `cas send`,
  where a conversation lives (`projects/<resolved folder>/<session>.jsonl`, `file-history/<session>/`)
  and the `CLAUDE_CODE_SESSION_ID` a session gives its commands. It was
  checked against 2.1.330. If an update changes one of them, `cas` falls back to the remaining
  sources, and `cas doctor` shows what broke.
- Each account logs in normally from this machine; `cas` doesn't hide or spoof anything. Check that
  the way you use several subscriptions fits Anthropic's terms for your plans.
- `cas uninstall` removes the status line, `/accounts`, the schedules, the `claude` shell function and
  the `cas` link. Profiles stay; `cas remove <name> --purge` logs one out and deletes it.

## Development

```sh
python3 -m unittest discover -s tests -t .
```

Tests run in a throwaway `HOME`. Override locations with `CAS_HOME`, `CAS_HUB_DIR` and `CAS_HUB_JSON`.

## License

MIT, see [LICENSE](LICENSE).
