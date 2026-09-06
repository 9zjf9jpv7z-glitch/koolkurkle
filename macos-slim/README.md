# macos-slim (Heavy packet 20260905-02)

SIP-safe slimming of Photos / media-analysis / Duet Expert agents on the
**Mac Mini M4 24GB** (macOS Tahoe ~26.3). This is the Mini-only install
target. Do **not** install on the MBP by default.

Canonical tree matches the packet installed on the Mini. User scripts live
here; the optional root helper is mdutil-only.

## SIP stays on

- No `csrutil`
- No sealed-volume mounts
- No moving Apple plists out of `/System`

Do **not** touch: `softwareupdated`, XProtect, `syspolicyd`, MRT, Find My,
WindowServer, FileVault, or the firewall.

Default slim set (all four required):

- `com.apple.mediaanalysisd`
- `com.apple.photoanalysisd`
- `com.apple.photolibraryd`
- `com.apple.duetexpertd`

Do **not** add `coreduetd`, `dasd`, `suggestd`, `sharingd`, `rapportd`, or
`useractivityd`.

## Default after install: mode=off

`install` writes the login LaunchAgent, links `~/bin/macos-slim`, and
appends `export PATH="$HOME/bin:$PATH"` to `~/.zshrc` only if that line is
absent. It sets `mode=off` if missing. It does **not** apply.

Do not `arm` or `persist` unless the operator at the machine asks.

## State and tick

State dir: `~/Library/Application Support/macos-slim/`

| file | values |
|---|---|
| `mode` | `off` \| `armed` \| `slim` \| `persist` |
| `session` | `kern.bootsessionuuid` |

Login LaunchAgent `com.user.macos-slim` (`RunAtLoad` + `StartInterval` 300)
runs `macos-slim.sh tick`:

| mode | session | action |
|---|---|---|
| `armed` | — | apply, `mode=slim`, capture session |
| `slim` | same as current boot | re-apply |
| `slim` | new boot | restore, `mode=off` |
| `persist` | — | apply, update session |
| else | — | no-op |

`persist` survives reboot. `armed` / `slim` restore on the next boot.

## Commands

```zsh
macos-slim install       # LaunchAgent + PATH; mode=off; does not apply
macos-slim status
macos-slim arm           # slim until next reboot
macos-slim persist       # slim across reboots
macos-slim restore-now   # restore now; mode=off
macos-slim disarm        # restore if applied; mode=off
macos-slim uninstall
```

`tick` is the LaunchAgent entry. Also accepted: `install`, `uninstall`.

## User install (Mini, login Buck)

Copy this tree onto the Mini (do not run from the MBP as the default
target). Scripts must be `chmod 700`; plists `644`.

```zsh
cd /path/to/macos-slim
chmod 700 macos-slim.sh apply.sh restore.sh root/macos-slim-root.sh root/INSTALL-ROOT.sh
chmod 644 com.user.macos-slim.plist.template root/com.user.macos-slim-root.plist
./macos-slim.sh install
./macos-slim.sh status
```

`install` generates `~/Library/LaunchAgents/com.user.macos-slim.plist` from
the template (`__MACOS_SLIM_SH__` / `__HOME__`) and bootstraps the gui
domain.

## Optional root helper (mdutil only)

The root helper never `launchctl`s agents. It only runs `mdutil`:

- `apply` — `mdutil -d` on `/`, `/System/Volumes/Data`, and non–Time Machine `/Volumes/*`
- `restore` — `mdutil -i on` on the same volumes
- `boot` — read the login user's `mode`; apply when `slim` or `persist`

Time Machine destinations are skipped via `tmutil destinationinfo`.

LaunchDaemon `com.user.macos-slim-root` is `RunAtLoad` only and calls the
helper with `boot`.

```zsh
cd /path/to/macos-slim/root
# helper 700 root:wheel → /usr/local/libexec/
# plist 644 → /Library/LaunchDaemons/; bootstrap system domain
sudo ./INSTALL-ROOT.sh
# optional sudoers.d (USERNAME replaced with the invoking user):
# INSTALL_SUDOERS=1 sudo ./INSTALL-ROOT.sh
```

sudoers **template only** (`root/macos-slim.sudoers.example`) — placeholder
`USERNAME`, not a password or a live account secret:

```
USERNAME ALL=(root) NOPASSWD: /usr/local/libexec/macos-slim-root.sh
```

`Buck` is an acceptable example name in docs. Nothing in this tree is a
Keychain secret, app password, or live sqlite DB.

If `/usr/local/libexec/macos-slim-root.sh` is executable, `apply.sh` /
`restore.sh` call it (or `sudo -n` when the file exists but is root-only).

## Sticky GUI (not automated)

These stay manual in System Settings — macos-slim does not flip them:

- Apple Intelligence & Siri: off
- Spotlight → Search Privacy (exclude volumes / folders as needed)
- Analytics / privacy toggles

## Permissions

| path | mode |
|---|---|
| `macos-slim.sh`, `apply.sh`, `restore.sh` | `700` |
| `root/macos-slim-root.sh`, `root/INSTALL-ROOT.sh` | `700` |
| installed helper `/usr/local/libexec/macos-slim-root.sh` | `700` root:wheel |
| plists (template, LaunchAgent, LaunchDaemon) | `644` |

## Not part of macos-slim

`com.mailroom.cull-photos` (or similar) remains a **separate** 60s `pkill`
during embeds. It is not installed, armed, or restored by this packet.
