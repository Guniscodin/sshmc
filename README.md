# SSHMC Panel

SSHMC is a local web panel for creating and managing Minecraft server profiles. It can install Vanilla, Fabric, Forge, and NeoForge servers; synchronize selected server folders from Git; install extra mods from HTTPS links; and manage server startup, logs, backups, and profile files.

## Features

### Minecraft server profiles

- Create multiple independent server profiles, each with its own Minecraft version, server build, loader version, Java executable, RAM allocation, game port, and data files.
- Supported builds are Vanilla, Fabric, Forge, and NeoForge. For modded builds, the panel looks up compatible loader versions and lets you choose one.
- Choose a Java executable by name or path. The default is `java`.
- RAM is configurable from 512 MB to 32,768 MB; the default is 2,048 MB.
- Enter a game port or leave it blank for automatic assignment. SSHMC also reserves a separate RCON port and checks for conflicts with other profiles and ports in use.
- Starting a profile installs its server software if needed, writes the server properties for its ports, accepts the Minecraft EULA, and launches the server.
- The dashboard shows the number of profiles, running servers, and RAM allocated to running servers.

### Server lifecycle

- **Start** runs the profile's synchronization and installation steps, then launches Minecraft.
- **Restart and sync** stops a running server, synchronizes its repository and configured mods, then starts it again.
- **Stop** first tries the server's local RCON interface, then escalates to operating-system signals if needed.
- While a server is starting, **Cancel Startup** stops the Java process and its launcher process. It preserves downloaded mods and existing world files. If Minecraft has already begun generating a world, cancellation can leave a partial world.
- Startup readiness is monitored after the launch request returns, so a slow or failed Minecraft startup does not hold the browser request open for five minutes. SSHMC reports asynchronous startup failures in the dashboard; detailed Minecraft output remains available in the server log.
- Missing mandatory mod dependencies are startup errors. SSHMC stops the failed startup instead of offering to continue without the dependency. For loader errors such as Fabric's missing dependency report, Minecraft fails mod resolution before world preparation.
- If the SSHMC panel exits while a server is still starting, it terminates that startup process group. Servers already running are detached and are not automatically stopped when the panel exits; stop them from the dashboard first if you want them shut down.

### Git repository synchronization

- A profile can connect to a Git repository over HTTP(S) or SSH. The repository must be reachable by the machine running SSHMC (for SSH, its Git credentials or SSH key must be available to that user).
- SSHMC synchronizes only these repository folders into the server directory:
  - `mods/`
  - `config/`
  - `datapacks/`
- Repository changes are applied during Start or Restart. Files removed from those folders in Git are removed from the server's corresponding synchronized folder.
- An empty public repository, or a repository with no remote branch yet, is treated as having no files to sync. The server can still be installed and started.
- Repository sync does not run `git clean`; unrelated files such as worlds are not deleted by Git synchronization.

### Reusable mod pack library

- Drag a ZIP onto the Mod Pack Library section or choose it with the file picker. The browser shows upload progress and lets you cancel. The ZIP streams into the configured data directory rather than being loaded whole into memory.
- Choose individual JARs or an entire folder instead. Folder imports flatten JARs from nested directories and preserve any config/ and datapacks/ content. Up to three files upload at once, with per-file progress, cancellation, and one automatic retry.
- Create a new pack or add uploaded files to an existing one. If an incoming JAR has the same filename as one already in the pack, SSHMC asks before replacing it. Expand a pack to see JAR sizes and detected loaders, or remove a JAR from the browser; linked profiles apply pack changes on their next Start or Restart.
- After upload, name the pack and choose its intended loader. SSHMC safely stages the import before publishing it, rejects unsafe ZIP paths and symbolic links, checks JAR archives, and warns about skipped non-JAR files, invalid JARs, unknown loader metadata, and loader mismatches.
- ZIPs may contain JARs at the root, in `mods/`, or inside one wrapper directory. `config/` and `datapacks/` are imported when present. JARs are flattened into the pack's `mods/` folder.
- Packs are stored globally under `modpacks/` and can be linked to a profile in its edit form. A profile can use one pack. On Start or Restart, Git sync runs first; the linked pack then replaces all JARs in the server's `mods/` folder and overlays its `config/` and `datapacks/` folders; direct-link mods run last.
- The editor warns about JARs whose detected loader differs from the profile and JARs with no supported loader metadata. This is a warning, not a startup block.
- Replacing a pack requires an explicit choice. Deleting a pack unlinks profiles that used it. Changing a profile's build or version keeps its pack link, though the pack may need a compatibility review.

### Additional mod download links

- Add up to 200 direct mod links to a profile, alongside its Git repository. Links must use public HTTPS URLs and point to JAR/ZIP-compatible mod files.
- An optional filename and SHA-256 checksum can be supplied for each link. SSHMC validates the downloaded archive and verifies the checksum when one is provided.
- Each file has a 1 GiB download limit. Downloads are processed sequentially after Git synchronization and copied into the server's `mods/` folder.
- Successfully downloaded files are cached locally. Later starts reuse the cache; syncing the Git repository does not re-download these files. Use **Forget cache and retry links next start** to clear the cache.
- If a link download or installation fails, SSHMC asks whether to continue without that link or stop startup. Stopping preserves repository files and already downloaded mods. Continuing remembers that link as skipped on future starts until you edit it or choose **Retry on next start**.
- Changing or adding a link does not restart a running server. Save the profile, then choose Start or Restart when you want the change applied.
- A missing mod download can affect world compatibility if the mod changes terrain or world data. SSHMC cannot determine in general whether a mod is safe to omit; review the failed link before choosing to continue.

### Version changes and backups

- Changing a profile's build, Minecraft version, or loader version marks its current server installation as incompatible. Stop the server before changing these settings.
- Saving such a change disconnects the old Git repository and clears that profile's configured direct mod links and download cache. Add the repository and links compatible with the new version afterward.
- On the next Start, SSHMC backs up the `mods/`, `config/`, and `datapacks/` folders before replacing incompatible server files.
- The edit form includes **Back up world folders before replacing this server**. If checked, the backup also includes `world/`, `world_nether/`, and `world_the_end/`. If unchecked, those world folders are removed during the replacement.
- Backups are compressed archives saved under the profile's `backups/` directory. A backup error or insufficient free space is reported as a warning and does not block startup.
- Each profile can create a smart backup on Restart (on by default). The newest five are retained by default, with a 10 GiB per-profile size cap; the newest backup is always kept even if it alone exceeds the cap. A pre-wipe backup is still attempted before an incompatible server replacement.
- SSHMC estimates the backup's uncompressed input size before writing. If the backup filesystem has less than that estimate plus 1 GiB free, it skips the backup, reports a warning, and continues startup.
- The profile editor can exclude regenerable cache/, crash-reports/, and logs/ folders from backups (enabled by default). The dashboard lists each backup's name, date, and size, with individual delete controls.

### Console and logs

- Open a profile's console to watch its live server log, including loader output and startup errors.
- Choose how many recent lines to keep visible: 100, 500, 1,000, 2,000, or 5,000. The selection is saved in that browser. Minecraft output rotates at 10 MiB and keeps five older files. On every Start, SSHMC also prunes the server's own compressed logs to the newest ten and removes crash reports older than 14 days, keeping the newest three.
- Panel logs are written to the terminal and to a rotating data/logs/panel.log file, with three older files kept. Routine dashboard polling requests are filtered from the HTTP access log; errors remain visible.
- The console shows server output; it is not an interactive command terminal.

### Monitoring

- The dashboard samples host CPU, per-core CPU, load average, RAM, and mounted filesystem usage every 3 seconds. It also shows CPU and RSS for each tracked running Minecraft process and its child processes.
- Host CPU and RAM sparklines keep the most recent 10 minutes in memory. Nothing from monitoring is written to the database or disk, and monitoring does not modify server files.
- Server, world, and backup folder sizes are scanned in the background every 60 seconds. The dashboard reports the combined world folders and the backups folder separately for each running profile.
- Disk rows include the filesystem containing `MCPANEL_DATA_DIR` and other mounted filesystems; common pseudo filesystems such as `tmpfs`, `overlay`, and `squashfs` are omitted.
- RAM bars turn yellow below 3 GiB available and red below 1.5 GiB. Disk bars turn yellow above 80% used and red above 90%. These thresholds are defined together in the monitoring service.
- Starting or restarting a profile whose configured RAM exceeds currently available host RAM displays a warning, but does not block the operation.

Monitoring API endpoints, protected by the panel login session:

- `GET /api/monitoring/stats` — latest host, running-server, filesystem, and cached profile-folder-size metrics.
- `GET /api/monitoring/history` — in-memory host CPU/RAM and running-server CPU/RSS samples from the last 10 minutes.

### Profile and file management

- Edit a profile's settings, repository, and mod links. Loader-version options update when the build or Minecraft version changes.
- Delete a profile from the panel while keeping its files, or check the permanent-delete option to remove the profile's server files, backups, logs, repository, and mod cache too.
- SSHMC stops a tracked server before deletion. If the server cannot be stopped safely, deletion is blocked so its files are not removed while the process is still running.

### Panel login

- On first launch, SSHMC asks for a panel password in the terminal. It requires at least 12 characters and stores a salted scrypt hash, not the password itself.
- On later launches, enter the password in the terminal to unlock the panel. The browser sign-in uses the same password.
- Login sessions use an HTTP-only, same-site cookie and expire after 12 hours. The local authentication file is `data/panel_auth.json` and is created with owner-only file permissions.
- Run SSHMC from an interactive terminal so it can prompt for the password. If you lose the password, stop the panel and remove `panel_auth.json`; the next interactive run will let you set a new password. This also invalidates existing sessions.

## Requirements

- Python 3.10 or later
- Java installed on the host for the selected Minecraft server version
- Git installed and configured if you use repository synchronization
- Network access for fetching loader versions, server software, repositories, and direct mod links

## Install and run

From the project directory:

```sh
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
uvicorn mcpanel.main:app --app-dir src --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000> in a browser. The default bind address makes the panel available only from the local machine. The default Uvicorn setup uses one worker; avoid `--reload` or multiple workers for normal use because each process may prompt for the panel password separately.

For access from another device, use an SSH tunnel or put the panel behind HTTPS with suitable access controls. Do not send the login password over an untrusted plain-HTTP connection. If you change the bind address to expose SSHMC on a network, protect that network endpoint before doing so.

Press Ctrl+C to stop the panel. Startup processes are cleaned up during panel shutdown; already-running Minecraft servers are detached, so stop those from the dashboard first if you want to stop them too.

## Data locations

By default, persistent data is stored in the project's `data/` directory:

- `panel.db` — profiles and runtime state
- `panel_auth.json` — password hash, salt, and session-signing key
- `profiles/<profile-id>/server/` — installed server, mods, configuration, and worlds
- `profiles/<profile-id>/repo/` — local Git checkout for the profile
- `profiles/<profile-id>/downloaded_mods/` — cached direct-link downloads
- `profiles/<profile-id>/logs/latest.log` and .1 through .5 — Minecraft server output and rotated files shown in the live console
- `profiles/<profile-id>/server/logs/` — Minecraft's own server logs; old compressed files are pruned on Start
- `profiles/<profile-id>/backups/` — compressed smart backups, pruned by count and total size
- `logs/panel.log` — rotating SSHMC application log
- `modpacks/<pack-name>/` — imported reusable packs, independent of profile directories
- `uploads/` — temporary ZIP uploads and streamed file/folder upload sessions

Set `MCPANEL_DATA_DIR` before launching SSHMC to store the database, authentication file, profile data, logs, and backups elsewhere. Back up this directory if you want to preserve profile data and login configuration.

Upload and storage limits can be adjusted with environment variables. Byte values are integer bytes:

- `MCPANEL_MAX_UPLOAD_BYTES` — maximum ZIP upload (default: 4 GiB).
- `MCPANEL_MAX_SINGLE_MOD_BYTES` — maximum individual JAR (default: 1 GiB).
- `MCPANEL_MAX_EXTRACTED_BYTES` — maximum expanded pack or staged file size (default: 8 GiB).
- `MCPANEL_UPLOAD_SESSION_MAX_AGE_SECONDS` — expiry for abandoned file/folder sessions (default: 86,400 seconds).
- `MCPANEL_SERVER_LOG_MAX_BYTES` and `MCPANEL_SERVER_LOG_BACKUP_COUNT` — Minecraft output rotation (defaults: 10 MiB and 5 files).
- `MCPANEL_SERVER_ROTATED_LOG_KEEP` — compressed server logs to retain (default: 10).
- `MCPANEL_CRASH_REPORT_RETENTION_DAYS` and `MCPANEL_CRASH_REPORT_KEEP` — crash report age and newest reports retained (defaults: 14 days and 3).
- `MCPANEL_PANEL_LOG_MAX_BYTES` and `MCPANEL_PANEL_LOG_BACKUP_COUNT` — panel file log rotation (defaults: 5 MiB and 3 files).
- `MCPANEL_BACKUP_RETENTION_COUNT` and `MCPANEL_BACKUP_MAX_BYTES` — per-profile backup count and size cap (defaults: 5 and 10 GiB).
- `MCPANEL_BACKUP_MIN_FREE_BYTES` — free space kept in addition to estimated backup input size (default: 1 GiB).

Upload sessions older than 24 hours are cleaned hourly and at panel startup. ZIP imports remain available alongside streamed JAR and folder uploads.

## Platform support

SSHMC targets Linux and macOS process groups, signals, and filesystem behavior. Windows is not currently supported.

## Troubleshooting

- **Panel exits because it needs a password:** start it from an interactive terminal. On the first run, create a password; on later runs, enter it when prompted.
- **No compatible loader versions appear:** check the Minecraft version and network connection, then retry. The loader selector is populated from the corresponding loader service.
- **A Git repository fails to sync:** confirm the URL is correct and reachable from the host. For SSH URLs, ensure the user running SSHMC has the right SSH key and host access. An empty repository is valid and syncs no files.
- **A linked mod fails to download:** check that its URL is public HTTPS, the file is a valid JAR archive under 1 GiB, and any supplied checksum is correct. The prompt lets you stop or continue without the failed download.
- **Minecraft reports a missing or incompatible mod:** open the profile console to inspect the loader's error. A required dependency error stops startup; install the missing compatible dependency and retry. SSHMC does not offer to skip a dependency error.
- **The server does not become ready:** inspect the profile console and confirm Java is installed and the configured RAM is available. SSHMC marks failed or timed-out startup as crashed and terminates the startup process group.
- **The panel cannot delete a profile:** it may still have a live server process. Stop the server and retry; SSHMC refuses to remove profile files while a tracked process may still be using them.

## Project layout

```text
src/mcpanel/              FastAPI application, lifecycle services, and web UI
src/mcpanel/api/          Profile, lifecycle, and console endpoints
src/mcpanel/core/         Git sync, loader install, mod downloads, RCON, runtime
src/mcpanel/web/          HTML templates and static styles
tests/                    Test package and fixtures
data/                     Runtime data (created automatically; not source code)
```
