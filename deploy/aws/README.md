# Running Citadel on AWS (one small EC2 instance)

`bootstrap.sh` sets up everything on a fresh Ubuntu 24.04 (arm64) instance: 2 GB swap, Docker,
the code, Postgres + the app + Caddy (HTTPS, optional password), and a daily database backup.
It was tested here as an isolated docker stack (pages through Caddy, no errors, a backup dump),
not on a real EC2 instance, so expect to read the log on your first launch:
`/var/log/citadel-bootstrap.log`.

## Is t4g.small free?

Not in the old sense, and I would not rely on it being free. Check your own account's Free Tier
page, because AWS changed the rules in 2025:
- The classic 12-month free tier covered **t2/t3.micro (1 GiB)**, which is far too small for this.
- A free trial of t4g.small ran until the end of 2025 and has finished.
- Newer accounts get a time-limited **credits** plan (about six months) in which t4g.small is an
  eligible type, but it **spends the credits**; it is not free.

Expect roughly **$12-13/month** for the instance, **$2-3** for a 30 GB disk and about **$3.60** for the
public IPv4 address (AWS charges for it), plus any CPU-credit overage (see below). About $20/month.

## Honest sizing

Measured on the isolated test stack (almost empty database): the app container used **~1.1 GiB**,
Postgres ~90 MiB (it grows with the data), Caddy ~15 MiB, plus the OS and Docker. That is about
1.8-1.9 GiB on a 2 GiB machine, so a t4g.small will lean on swap and may be slow during recompute
spikes. It can run; it will not be comfortable. **t4g.medium (4 GiB, ~$25/month) is what I would
start with**, and the same script works unchanged. You can resize later (stop, change type, start;
the data lives on the disk).

CPU: the app uses about one core continuously, and burstable (t) instances only earn credits at
about 20% of a core per vCPU. They run in "unlimited" mode by default, which bills the overage
(about $0.04 per vCPU-hour). If that bill grows, a non-burstable c7g.large (~$50) costs about the same.
Watch the logs: `docker compose -p citadel logs citadel | grep "stack stages"` should show
compute in the single seconds; if it is regularly above ~10 s the instance is too small.

## Launch

1. Launch an instance: **Ubuntu Server 24.04 LTS (arm64)**, type **t4g.small** (or t4g.medium),
   **30 GB gp3** disk, an **IAM role with `AmazonSSMManagedInstanceCore`** (lets you open a shell from
   the console with Session Manager, so you do not need SSH), and a security group that allows only
   **inbound 80 and 443**.
2. Edit the CONFIG block at the top of `bootstrap.sh`, then paste the whole file into
   **Advanced details > User data** (or run it as root on the box):
   - `GITHUB_TOKEN`: a read-only fine-grained token for this repo (it is private);
   - `BASIC_AUTH_USER` / `BASIC_AUTH_PASSWORD`: **set these**, the app has no login of its own;
   - your `ENTSOE_KEY` and IRIS keys, if you have them;
   - `SITE_ADDRESS`: leave as `:80` for plain HTTP on the IP, or put a domain name whose DNS A record
     already points at the instance's Elastic IP for automatic HTTPS.
   User data is readable by anyone with access to the instance's metadata, so treat the instance as
   holding those secrets.
3. Give the instance an **Elastic IP** so its address survives a stop/start.
4. Wait 5-10 minutes (the first Docker build is slow on a small instance), then open
   `http://<ip>/fpn`. The Natgrid page fills in as the 14-day backfill runs.

## Running it

- **Update:** `sudo /opt/citadel/deploy/aws/update.sh`
- **Logs:** `docker compose -p citadel logs -f citadel`
- **Everything restarts by itself** after a reboot (`restart: unless-stopped`, Docker enabled at boot).
- **Backups:** a timer dumps the database daily at 03:30 UTC to `/var/backups/citadel` (7 kept) and,
  if `BACKUP_S3_BUCKET` is set and the instance role allows it, to S3. Restore command is in `backup.sh`.
- **Disk growth:** `storage/retention.py` deletes old rows every few hours (settings
  `RETENTION_DAYS_STACK` 14, `_FPN` 7, `_LOG` 7, `_TELEMETRY` 30, `_NATGRID` 90, `_FUNDIES` 90; 0 keeps
  forever). Trips and REMIT history are never pruned.
- **Profiles:** `PROFILE` in the CONFIG block of `bootstrap.sh`. `small` (default, for a 2 GiB t4g.small):
  1 recompute worker, BM Stack page off, 10 s polling, 3-day natgrid backfill, 4 GiB swap; measured at
  about 0.9 GiB for the app, but on an almost empty database. `standard` (4 GiB or more): 2 workers, BM
  Stack on, 5 s polling, 14-day backfill. To change later, edit `/opt/citadel/.env.aws` and run `update.sh`.
- **Not set up:** monitoring/alerts (add a CloudWatch alarm on CPU credits and disk), and nothing here
  protects against the AWS account itself being the single point of failure.
