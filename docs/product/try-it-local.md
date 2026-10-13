# Try Quadringent on one machine

```sh
quadringent local init   --ibmi-host ibmi.example.test --ibmi-user COPYUSER \
  --table SALESLIB/ORDERS --snowflake-account myorg-myaccount
quadringent local snowflake-script       # give the output to a Snowflake administrator
quadringent local check --signon --discover --snowflake
quadringent local start
```

`quadringent local` copies **one** IBM i table to Snowflake and keeps it
current, from your laptop or one VM, with Docker. There is no cloud account to
create, no Kubernetes and no control plane to install.

**Scope.** This is a DEV/INT preview to see the product work on a real table.
It is not a site qualification and it does not prove continuity, latency or
production readiness for your site. The managed install
([default installer](install-default.md)) remains the supported way to run
Quadringent for real.

## What you need

| Need | Detail |
| --- | --- |
| Docker | A running Docker daemon on a Linux host, or in a Linux VM or WSL2 (inside the Linux file system). Docker Desktop on macOS or Windows is not enough on its own, see [limits](#limits). Native Windows is not supported. |
| Python 3.12 | To install the `quadringent` command from the wheel of an admitted release (see [Release assets](install-default.md#release-assets)). |
| An IBM i | Reachable from this machine and from Docker over TLS (ports 9471, 9475, 9476 and 449, or the three ports your administrator fixed). A user that can read the catalog, the journal receivers and the table. |
| A Snowflake account | A Snowflake administrator who runs one SQL script once. You never give Quadringent an administrator credential. |
| Disk | Local Linux disk (ext4, xfs or btrfs) for the data directory. See [limits](#limits). |

The images are `ghcr.io/quadringent/quadringent-ce:<version>-capture` (IBM i
reader and jobs) and `ghcr.io/quadringent/quadringent-ce:<version>-control-plane`
(Snowflake loader), where `<version>` is the version of the installed command.
`--capture-image` and `--loader-image` replace them, for example with a private
mirror or a digest (`name@sha256:...`).

The table must be journaled with `IMAGES(*BOTH)` and must have a primary key
or a unique key. The tool tells you when it is not, and prints the exact CL
commands for your IBM i administrator to review and run. It never runs them.

## Step by step

### 1. Create an instance

```sh
quadringent local init --name sales \
  --ibmi-host ibmi.example.test --ibmi-user COPYUSER \
  --table SALESLIB/ORDERS \
  --snowflake-account myorg-myaccount
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
| `data/` | The shared data directory (checkpoints, copied data, console snapshot). |
| `state/` | Which steps are done, so a second `start` resumes. |

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

`start` shows a summary and asks to continue (`--yes` skips the question). Then
it:

1. pulls the images if needed and proves that the data directory file system is
   supported;
2. signs on once and derives the capture time zone from the IBM i;
3. reads the catalog entry of the table and stops with CL commands if it is
   not ready;
4. records the journal position, then copies the table once;
5. starts a **capture** container, which follows the journal, and a **loader**
   container, which writes the history and mirror tables in Snowflake.

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

**Costs.** The loader uses a dedicated `XSMALL` warehouse
(`QDT_WH_<suffix>`) created with `AUTO_SUSPEND = 60` and `AUTO_RESUME`: it
bills only while loading, with a 60 second minimum each time it resumes.
Storage of the two tables is billed as usual. Docker pulls, if any, are billed
by your registry and network.

## Stop and clean up

```sh
quadringent local stop  --name sales    # containers stop; checkpoints are kept; start resumes
quadringent local reset --name sales --yes
```

`reset` stops the containers and deletes the data directory and the recorded
steps. It keeps the configuration and the secrets. It does **not** touch
Snowflake: to copy the same table again from scratch, also drop the history and
mirror tables yourself.

To change the host, user, certificate authority, ports, time zone or images
of an instance, delete `~/.quadringent/local/<name>/` after `reset --yes` and
run `init` again. A new instance generates a new Snowflake key, user and role,
so the administrator must run the new script. (`init` refuses an existing name.)

To remove everything: run `reset`, delete `~/.quadringent/local/sales/`, drop
the Snowflake objects the script created (service user, role, warehouse,
database or schemas, read role), and remove the images with `docker image rm`.

## Limits

* **DEV/INT preview.** Not a site qualification, no continuity or latency
  claim.
* **One table per instance.** The table needs a primary or unique key and
  journaling with `IMAGES(*BOTH)`. Tables identified only by their physical
  position (RRN) are not supported. Use several instances for several tables.
* **One machine.** The containers share one data directory. No clustering, no
  failover.
* **Clear text on disk.** The data directory holds the rows of the table in
  clear text, and the instance directory holds the IBM i password and the
  Snowflake key as 0600 files. Protect the folder, use disk encryption, and
  delete it when you are done.
* **Supported file systems only.** Use a local disk. Not supported: NFS, SMB,
  FUSE and cloud-synced folders (Dropbox, iCloud Drive, OneDrive), FAT and
  exFAT, `tmpfs`, and `/mnt/c` or other Windows drives under WSL2. `start`
  runs a gate in a container that checks hard links, locking between
  containers, atomic rename and directory sync, and refuses to continue if the
  folder fails it. Docker Desktop on macOS and Windows shares host folders
  through a virtual file system that has been seen to grant the same file lock
  to two containers, so the gate refuses it even on APFS or NTFS: run on a
  Linux host, or inside a Linux VM or WSL2 with the instance on its own Linux
  disk.
* **Native Windows is not supported.** Use WSL2 and keep the instance in the
  Linux file system (`~`, not `/mnt/c`).
* **TLS by default.** `--allow-plaintext-ibmi` exists for isolated networks
  only: the password and the data cross the network unencrypted, a warning is
  printed by `init`, `check` and `start`, it needs `--source-time-zone`, and it skips the
  probe, which requires TLS. It uses ports 8471, 8475, 8476 and 449.
* **Not a service.** There is no web console. Containers are restarted a few
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
Quadringent developers only: the result is **not reproducible**, depends on the
state of the checkout, and proves nothing about a release. Do not use it to
evaluate the product.

## Troubleshooting

| Message | Meaning and action |
| --- | --- |
| `unsupported file system` | Move the instance to a local disk, see [limits](#limits). |
| `IBM i sign-on refused` | Wrong user or password, or a disabled profile. Nothing was retried. Ask the administrator, then run again. |
| `certificate is not trusted` | Pass the public certificate of the authority with `--ibmi-ca-file` (at `init`: see [Stop and clean up](#stop-and-clean-up) to recreate the instance). |
| `the table is not ready` | Run the printed CL commands through your IBM i administrator, then `start` again. |
| `time zone could not be derived` | Set `--source-time-zone` (an IANA name such as Europe/Paris) at `init`, on a recreated instance. |
| `another quadringent local command is running` | Wait for it, or stop it. |
| `configuration changed since the recorded run` | `reset --yes`, or restore the previous settings. |
