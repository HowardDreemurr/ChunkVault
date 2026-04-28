# The wizard

Running `chunkvault` with no subcommand launches an interactive
[questionary](https://github.com/tmbo/questionary) + [rich](https://github.com/Textualize/rich)
TUI. It's the path of least resistance for everyday use.

```bash
chunkvault                          # ← no args, no subcommand
```

## What it does on launch

1. Loads your locale (see [i18n](i18n.md)) — defaults to system locale if
   one of the 9 supported languages is detected; falls back to English
   otherwise.
2. Reads the registry at `~/.chunkvault/config.json`. Two lists:
   - **Repos** — vault paths you've registered.
   - **Source paths** — directories you keep `.zip`/`.tar.gz` archives in.
3. Scans both for current state — counts snapshots, archive sizes, finds
   the newest source archive of each.
4. Renders a `rich.Table` summary and drops you into the main menu.

## The menu structure

The main menu is a flat-with-submenu hybrid:

```
chunkvault — main menu
  ─ Snapshot                ▸  (submenu: take, list, restore, delete, …)
  ─ Ingest archive          ▸  (submenu: ingest one, ingest folder, per-server vaults)
  ─ Inspect & diff          ▸
  ─ Health & verify         ▸  (verify, fsck, verify-roundtrip, verify-folders)
  ─ Repair                  ▸  (repair-timestamps, migrate-mca-files, retime)
  ─ Browse / visualize      ▸
  ─ Settings                ▸  (language, registry, …)
  ─ Quit
```

Each leaf is wrapped in a "press Enter to confirm, Esc to back out" prompt
so you can't fat-finger a destructive op. Long-running ops show live
progress via `rich.progress.Progress`; the bar updates at every
`phase_progress` event from the underlying API.

## Per-server vault dispatch

The wizard's "ingest archive" flow detects when an archive contains
multiple `<server>/world/` directories and asks how to route them:

- **Bundled vault.** All servers go into the current vault. Cross-server
  dedup happens, but a delete of `EX-Server`'s data has to walk
  `CR-Server`'s manifests.
- **Per-server vaults.** Each server goes into its own vault
  (`F:/Vaults/EX-Server`, `F:/Vaults/CR-Server`, …). The wizard offers to
  create vaults for new servers and saves the mapping to the registry. The
  archive is extracted once and dispatched in one pass.

We recommend per-server vaults for production use — the blast-radius
benefit is significant, and the cross-server-dedup saving is usually
small in practice.

## Adding repos and sources from the wizard

Settings ▸ Registry lets you:

- Add a vault path (with optional friendly label).
- Remove a vault path.
- Add a source-archive directory.
- Remove a source-archive directory.

The same operations are available from the CLI:

```bash
chunkvault repo   list
chunkvault repo   add    F:/Vaults/EX-Server  --label EX-Server
chunkvault repo   remove F:/Vaults/EX-Server

chunkvault source list
chunkvault source add    /backups
chunkvault source remove /backups
```

Anything you change in the wizard persists; the next CLI run sees it, and
vice versa.

## Switching language at runtime

Settings ▸ Language opens a list of all 9 locales. The selection takes
effect immediately — the menu redraws in the new language without
restarting the wizard. Your choice is saved to the registry and used on the
next launch.

## Headless modes

Most operations the wizard exposes are also available as raw CLI
subcommands (see [CLI reference](cli.md)). The wizard is just the discoverable
glue; nothing it does is unavailable to scripts.

## Wizard state files

| File                                | Purpose                                 |
| ---                                 | ---                                     |
| `~/.chunkvault/config.json`         | Repo + source registry, locale.         |

Nothing wizard-specific is stored inside the vault. A vault is portable
between users; the registry is per-user.
