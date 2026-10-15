# Try Quadringent on one machine

Local mode is **Unreleased** ([changelog](../../CHANGELOG.md)). The published
0.4.5 wheel does not include `quadringent local`, and compatible local-mode
images are not published yet. Use a reviewed source checkout and images built
from the same checkout. From its repository root:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install '.[api]'
docker build -f docker/Dockerfile -t quadringent-local-capture:unreleased .
docker build -f docker/control-plane.Dockerfile -t quadringent-local-loader:unreleased .

quadringent local init   --ibmi-host ibmi.example.test --ibmi-user COPYUSER \
  --table SALESLIB/ORDERS --snowflake-account myorg-myaccount \
  --capture-image quadringent-local-capture:unreleased \
  --loader-image quadringent-local-loader:unreleased
quadringent local snowflake-script       # give the output to a Snowflake administrator
quadringent local check --signon --discover --snowflake
quadringent local start
```

`quadringent local` copies one IBM i table to Snowflake and keeps it current,
from your laptop or one VM, with Docker. There is no cloud account to create, no
Kubernetes and no control plane to install.

It is meant to let you see the product work on a real table of your DEV IBM i.
We have not yet validated it end to end ([evidence](../evidence.md)). For more than
one table, or a setup hosted in your cloud account, use the
[default installer](install-default.md).

## What you need

| Need | Detail |
| --- | --- |
| Docker | A running Docker daemon: on a Linux host (data in a folder), or Docker Desktop on macOS, or on Windows with WSL2 (data in a Docker volume, see [Docker Desktop](#docker-desktop-macos-and-windows-with-wsl2); Not tested on a real Docker Desktop; the file system gate decides). Native Windows is not supported. |
| Python 3.12+ | To install the source CLI as above. The published 0.4.5 wheel does not provide local mode. |
| An IBM i | Reachable from this machine and from Docker over TLS (ports 9471, 9475, 9476 and 449, or the three ports your administrator fixed). A user that can read the catalog, the journal receivers and the table. |
| A Snowflake account | A Snowflake administrator who runs one SQL script once. You never give Quadringent an administrator credential. |
| Disk | A local Linux disk (ext4, xfs or btrfs) for the data directory, or the disk of Docker's Linux VM for a Docker volume, if it passes the file system gate. See [limits](#limits). |

The default image references are `ghcr.io/quadringent/quadringent-ce:<version>-capture`
(IBM i reader and jobs) and `ghcr.io/quadringent/quadringent-ce:<version>-control-plane`
(Snowflake loader), where `<version>` is the installed command's version.
The examples override these defaults with the two local images built above.
`--capture-image` and `--loader-image` can also select compatible images in a
private mirror or by digest (`name@sha256:...`).

The table must be journaled with `IMAGES(*BOTH)` and must have a primary key
or a unique key. The tool tells you when it is not, and prints the exact CL
commands for your IBM i administrator to review and run. It never runs them.

## Step by step

### 1. Create an instance

```sh
quadringent local init --name sales \
  --ibmi-host ibmi.example.test --ibmi-user COPYUSER \
  --table SALESLIB/ORDERS \
  --snowflake-account myorg-myaccount \
  --capture-image quadringent-local-capture:unreleased \
  --loader-image quadringent-local-loader:unreleased
```

The IBM i password is asked with a hidden prompt. For automation, use
`--ibmi-password-file FILE` or pipe it on stdin. It is never a command-line
value, so it stays out of your shell history and process list.

If your IBM i certificate is signed by a private authority, pass its public
certificate with `--ibmi-ca-file ca.pem`. Certificate verification is never
turned off. If the IBM i uses fixed ports, add `--database-port`,
`--signon-port` and `--command-port`.

This creates `~/.quadringent/local/sales/` (mode 0700):

| Path | Content |
| --- | --- |
| `config.json` | Settings, no secret. |
| `secrets/ibmi_password`, `secrets/snowflake_key.p8` | The only secrets, mode 0600. The Snowflake key is generated here and never leaves. |
| `snowflake-setup.sql` | The script for the administrator. It contains the public key only. |
| `data/` | The shared data directory (checkpoints, copied data, console snapshot), when the data location is `bind`. With `volume` there is no such folder: the data lives in the Docker volume `qdt-local-sales-data`. |
| `state/` | Which steps are done, so a second `start` resumes. |

The data location is chosen for you: `volume` when `docker info` reports
Docker Desktop or the host is macOS, `bind` otherwise (a Linux host, including
WSL2 when `docker info` does not report Docker Desktop). Force one with
`--data-location bind` or `--data-location volume`. `start` creates the volume;
`init` does not need Docker running, but then it cannot detect Docker Desktop
and says so. The location is fixed when the instance is created: to change it,
create another instance (see [Stop and clean up](#stop-and-clean-up)).

### 2. Have the Snowflake script run

```sh
quadringent local snowflake-script --name sales > setup.sql
```

A Snowflake administrator runs `setup.sql` once. It creates a service user
with your public key, a role, a small warehouse, a database and one or two
schemas, and a read-only role that your analysts can be granted. Nothing in it
reads existing data.

### 3. Check

```sh
quadringent local check --name sales --signon --discover --snowflake
```

Without options, `check` signs on nowhere and contacts no Snowflake: it
checks Docker, file permissions, TCP and TLS to the IBM i, and reports the rest
as not observed (exit code 3). Each option adds one remote check:

| Option | What it does |
| --- | --- |
| `--signon` | One IBM i sign-on from a container, which also reads the system time zone. |
| `--discover` | Reads the catalog entry of your table: journal, images, key, columns. |
| `--snowflake` | Connects as the service user and proves it can create tables, then drops its probe table. |

Exit codes: 0 ready, 1 blocked (a check failed), 2 usage error, 3 incomplete.
Add `--json` for the `quadringent.diagnostic.v1` report. A wrong password can
disable an IBM i profile: when a sign-on fails, nothing is retried. Fix the
password, then run the check once more.

### 4. Start

```sh
quadringent local start --name sales
```

`start` first:

1. pulls the images if needed, prepares the data volume when there is one (a
   short-lived helper container creates its marker), and proves that the data
   directory file system is supported;
2. signs on for a source probe and derives the capture time zone from the IBM i;
3. reads the catalog entry of the table and stops with CL commands if it is
   not ready.

These source checks are read-only and happen before the confirmation prompt.
It then shows a summary and asks to continue (`--yes` skips the question).
Only after confirmation does it record the journal position and admission.
It then copies the table once and starts a capture container, which follows
the journal, and a loader container, which creates and writes the history and
mirror tables in Snowflake.

If a step fails, fix the cause and run `start` again: finished steps are
skipped, and an authentication failure stops everything and is never retried
by the tool.

### 5. Watch

```sh
quadringent local status --name sales
quadringent local status --name sales --logs
```

`status` shows the containers, the recorded steps and the capture console
snapshot (state, lag in journal entries, events published, errors). It does not
claim what reached Snowflake: that count is not read back. Query the tables in
Snowflake to see the data, in `<database>.RAW.<TABLE>_HISTORY` and the mirror
table in `<database>.CURATED` (or both in the one schema you chose with
`--snowflake-schema`).

`start` does not connect to Snowflake before copying: only
`check --snowflake` proves that the administrator ran the script. Run that
check before `start`. If the script was not run, the loader container fails at
its first login and stops after a few restarts; `status --logs` shows the
reason. Have the script run, then run `start` again: the admission and the
initial copy are kept and only the loader starts again.

## What it does on the IBM i

Everything is read-only:

* catalog queries (table, columns, key, journal, receivers) and two system
  values (version and time zone);
* reading the journal receivers of the table's journal;
* one snapshot `SELECT` of the table during the initial copy.

It does not create, alter or delete anything on the IBM i, and it never changes
journaling. Sign-ons come from containers on this machine with the user you
gave.

## What it does in Snowflake

The administrator script creates the objects listed in step 2. The loader, as
the service role only, creates two tables per source table (history and
mirror), appends changes to the history and merges them into the mirror.

Costs: the loader uses a dedicated `XSMALL` warehouse
(`QDT_WH_<suffix>`) created with `AUTO_SUSPEND = 60` and `AUTO_RESUME`: it
bills while the warehouse is running, including idle time before auto-suspend,
with a 60 second minimum each time it resumes. Repeated resumes incur repeated
minimums. Storage of the two tables is billed as usual. Docker pulls, if any, are billed
by your registry and network.

Local mode does not enforce a credit budget. Before starting, have your
administrator attach a [resource monitor](https://docs.snowflake.com/en/user-guide/resource-monitors)
to this warehouse, with reviewed thresholds and suspension actions. A monitor
is not an exact spending cap: allow headroom for suspension delay and costs
outside warehouse compute.

## Stop and clean up

```sh
quadringent local stop  --name sales    # containers stop; checkpoints are kept; start resumes
quadringent local reset --name sales --yes
```

`reset` stops the containers and deletes the data directory (or removes the
Docker volume, then creates an empty one) and the recorded steps. It keeps the
configuration and the secrets. With a volume it removes only a volume that
carries both labels that this tool put on it (`io.quadringent.local=<name>` and
`io.quadringent.local.id=<the instance's own store label>`), so a volume of another
instance with the same name (another `--root`, or an instance deleted and created
again) is refused, and never touches a host folder. The next `start` creates a new
volume. It does not touch
Snowflake: to copy the same table again from scratch, also drop the history and
mirror tables yourself.

To change the host, user, certificate authority, ports, time zone or images
of an instance, delete `~/.quadringent/local/<name>/` after `reset --yes` and
run `init` again. A new instance generates a new Snowflake key, user and role,
so the administrator must run the new script. (`init` refuses an existing name.)

To remove everything: run `reset`, delete `~/.quadringent/local/sales/`, drop
the Snowflake objects the script created (service user, role, warehouse,
database or schemas, read role), remove the images with `docker image rm` and,
for a volume instance, the volume with `docker volume rm qdt-local-sales-data`.

## Docker Desktop (macOS, and Windows with WSL2)

Docker Desktop shares host folders with containers through a file-sharing layer
whose hard links and file locks do not behave like a local disk between two
containers, so a bind-mounted data folder fails the file system gate there. A
Docker named volume lives inside Docker's own Linux VM on its own disk, so
`init` uses one by default on Docker Desktop and on macOS:

* the volume is `qdt-local-<name>-data`, created by `start` with the labels
  `io.quadringent.local=<name>` and `io.quadringent.local.id=<store label>`, and
  mounted at `/data` in every container;
* short-lived helper containers of the capture image do what the command would
  otherwise do in the folder: create the marker, and read the capture console
  snapshot for `status` (`status` never pulls an image and never creates the
  volume). `reset` removes the labelled volume;
* the IBM i password and the Snowflake key stay private files on the host,
  mounted read-only. These single-file mounts (and the optional certificate
  authority file) still cross Docker Desktop's file-sharing layer, so the
  ownership and mode mapping there is Not tested, and the instance
  directory (`--root`) must be inside a folder that Docker Desktop shares;
* the gate that `start` runs (hard links, locks between containers, atomic
  rename, directory sync) still decides. This mode is Not tested on
  a real Docker Desktop: if the gate fails, `start` stops and says which check
  failed. On a Linux host use `--data-location bind`.

The business data stays in clear text inside the volume. The commands below
are illustrative, not a tested procedure: back it up or look inside with a
throw-away container (stop the instance first for a consistent copy; `alpine`
is only an example image, pin the one you trust, and pulling it needs a network):

```sh
quadringent local stop --name sales
docker run --rm -v qdt-local-sales-data:/data:ro -v "$PWD":/backup alpine \
  tar czf /backup/sales-data.tgz -C /data .
docker run --rm -v qdt-local-sales-data:/data:ro alpine ls -la /data
```

The archive holds the same clear-text rows: protect it like the data directory.
Restoring a backup into a new instance is not a supported workflow.

## Limits

* An instance replicates one table. The table needs a primary or unique key and
  journaling with `IMAGES(*BOTH)`; tables identified only by their physical
  position (RRN) are not supported. Use several instances for several tables.
* Everything runs on one machine: the containers share one data directory or one
  Docker volume, with no clustering and no failover.
* The data directory (or the Docker volume, which sits
  in Docker's storage) holds the rows of the table in clear text, and the
  instance directory holds the IBM i password and the Snowflake key as 0600
  files. Protect the folder and Docker's disk, use disk encryption, and delete
  them when you are done.
* Use a local disk or a Docker volume. Not
  supported: NFS, SMB, FUSE and cloud-synced folders (Dropbox, iCloud Drive,
  OneDrive), FAT and exFAT, `tmpfs`, and `/mnt/c` or other Windows drives under
  WSL2. `start` runs a gate in a container that checks hard links, locking
  between containers, atomic rename and directory sync, and refuses to continue
  if the data directory or volume fails it. Docker Desktop on macOS and Windows
  shares host folders through a virtual file system that has been seen to grant
  the same file lock to two containers, so the gate refuses a bind-mounted
  folder there even on APFS or NTFS: use the Docker volume (the default there),
  run on a Linux host, or inside a Linux VM or WSL2 with the instance on its own
  Linux disk. A Docker volume is still subject to the gate, and the gate, not
  the platform, decides.
* Native Windows is not supported; use WSL2 and keep the instance in the
  Linux file system (`~`, not `/mnt/c`).
* TLS is the default. `--allow-plaintext-ibmi` exists for isolated networks
  only: the password and the data cross the network unencrypted, a warning is
  printed by `init`, `check` and `start`, it needs `--source-time-zone`, and it skips the
  probe, which requires TLS. It uses ports 8471, 8475, 8476 and 449.
* This is not a service and has no web console. Containers are restarted a few
  times after a failure; do not rely on them coming back after a reboot or a
  Docker restart: run `start` again.

## Developer-only: `--from-checkout`

Until the next release publishes images that include local mode, a developer
can run the local mode code of a source checkout over the images of an earlier
version:

```sh
quadringent local init --name dev ... --from-checkout /path/to/checkout
```

The checkout is bind-mounted read-only over the code in the images. This is for
Quadringent developers only: the result is not reproducible, depends on the
state of the checkout, and proves nothing about a release. Do not use it to
evaluate the product.

## Troubleshooting

| Message | Meaning and action |
| --- | --- |
| `unsupported file system` | Move the instance to a local disk, or use a Docker volume, see [limits](#limits). |
| `Docker volume ... is not labelled`, or `belongs to another instance that has the same name` | A volume with the instance's name exists but was not created by this tool for this instance. `start`, `status` and `reset` refuse to use or delete it: rename or remove it yourself (`docker volume rm`), or use another instance name. |
| `cannot inspect the Docker volume` | Docker did not answer the volume query. Nothing was created or removed; check the daemon and run the command again. |
| `IBM i sign-on refused` | Wrong user or password, or a disabled profile. Nothing was retried. Ask the administrator, then run again. |
| `certificate is not trusted` | Pass the public certificate of the authority with `--ibmi-ca-file` (at `init`: see [Stop and clean up](#stop-and-clean-up) to recreate the instance). |
| `the table is not ready` | Run the printed CL commands through your IBM i administrator, then `start` again. |
| `time zone could not be derived` | Set `--source-time-zone` (an IANA name such as Europe/Paris) at `init`, on a recreated instance. |
| `another quadringent local command is running` | Wait for it, or stop it. |
| `configuration changed since the recorded run` | `reset --yes`, or restore the previous settings. |
