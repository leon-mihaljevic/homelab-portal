# Homelab Portal

A two-tier web application that tracks and monitors my actual homelab infrastructure — servers, VMs, and Docker services — with live data pulled from Proxmox and SSH. Built as the third project in a series exploring AWS cloud infrastructure, on-prem CI/CD, and containerized application delivery.

**Stack:** Flask + PostgreSQL, Docker, Jenkins, deployed on AWS EC2 with live data sourced from an on-prem Proxmox homelab over Tailscale.
![Homepage](docs/screenshot.png)

## What it does

- Tracks every server/VM in the homelab: hardware specs, IP, notes — full CRUD
- Tracks services running on each server, with port and notes — full CRUD
- Shows **real** live status per server, not a manual toggle:
  - If a Proxmox VMID is set, status and uptime come directly from the Proxmox API
  - Otherwise, falls back to a TCP port-check (port 22) as a basic reachability test
- Shows **real** live Docker container status and disk usage for the Docker host, pulled over SSH using a key that can do nothing except run those two specific commands
- Login-gated (session-based, hashed password) — not open to anyone who finds the IP
- Deploys itself: push to `main`, and within minutes the change is built, tested against a real throwaway database, pushed to a registry, and running — with zero manual steps

## Architecture

![Architecture diagram](docs/architecture.svg)

### Hybrid cloud design

The app runs on **AWS EC2 (t3.micro)** but its data sources live entirely on-prem:

```
Laptop → git push → GitHub → Jenkins (jenkins-01, on-prem)
                                    ↓
                              Build → Test → Push to Docker Hub
                                    ↓
                         AWS EC2 (homelab-portal) ←→ Tailscale ←→ Proxmox / docker-server
```

**Why not run everything on AWS**: the app's purpose is monitoring homelab infrastructure. The interesting architectural challenge and the one worth solving properly is making a cloud-hosted app reach securely back into private on-prem infrastructure, which is a real hybrid-cloud pattern used in production environments for exactly this scenario.

**Connectivity**: EC2 joins the same Tailscale tailnet as the homelab. All connections from EC2 to on-prem resources (Proxmox API, SSH agent) use Tailscale IPs — no port forwarding, no public exposure of on-prem services.

**Two VMs, one shared purpose (on-prem side):**
- `jenkins-01` — runs Jenkins in Docker, does nothing else. Kept deliberately separate from where the app actually runs, so a compromised build never has a direct path to the deploy target's other services.
- `docker-server` — hosts the existing homelab stack (Jellyfin, Uptime Kuma, Portainer) and serves as the source for live Docker container/disk data via SSH.

**Two-tier by design, not by default:**
- `db` (Postgres 16) holds the only genuinely persistent state — inventory facts, service notes, login credentials — in a named volume, with **no port published to the host**, reachable only from `web` over Docker's internal network.
- `web` (Flask + gunicorn) is stateless and disposable — every request either reads/writes the DB, or reaches out live to Proxmox/SSH, computes the page, and forgets everything.

## CI/CD pipeline

Every push to `main` runs a real four-stage Jenkins pipeline:

1. **Build** — `docker build`, tagged both with the Jenkins build number (for rollback) and `latest`
2. **Test** — spins up a throwaway Postgres and the newly built image on an isolated Docker network, polls the image's own `HEALTHCHECK` until it reports healthy (or fails loudly with logs if it never does), then tears everything down regardless of outcome
3. **Push** — only reached if Test passed; pushes both tags to Docker Hub using a token scoped to Read & Write only
4. **Deploy** — checks whether the EC2 instance is running (starting it via the AWS API if not, then waiting for real SSH reachability rather than just AWS's "running" state), then SSHes into EC2 over Tailscale using a forced-command-restricted key to trigger the redeploy

**Trigger mechanism is Poll SCM, not a GitHub webhook** — a deliberate choice: there's no port forwarding into this homelab (by design, in favor of Tailscale for remote access), so GitHub has no way to reach Jenkins directly. Jenkins polling GitHub every 5 minutes needs no inbound exposure at all, which is the same pattern real CI systems use behind a corporate NAT.

**EC2 auto-start**: since the instance runs only when needed (not 24/7), the Deploy stage first checks AWS state, starts the instance if stopped, waits for `instance-running` confirmation, then separately polls for actual SSH readiness — because AWS reporting "running" and the OS/SSH daemon being fully ready are genuinely different moments.

## Security model

Every credential in this project is scoped to the minimum it needs, not the account it came from:

| Credential | Scope |
|---|---|
| Proxmox API token | `PVEAuditor` role — read-only, can see status but can't start/stop/modify anything |
| SSH "agent" key (live Docker/disk data from docker-01) | Forced command — can only run `docker ps` and `df`, nothing else, no shell |
| SSH "deploy" key (Jenkins → EC2) | Forced command — can only run `ready` (health check) or `redeploy`, nothing else |
| IAM user `jenkins-ec2-starter` | `ec2:StartInstances` on this instance only + `ec2:DescribeInstances` (forced `*` by AWS) |
| Docker Hub token | Read & Write only, not admin |
| App login | Session-based, password stored as a werkzeug/scrypt hash, never plaintext |

Other decisions worth naming explicitly:
- The container runs as a non-root user (`appuser`), not root
- `gunicorn` in production, not Flask's dev server (which ships an interactive debugger — a real code-execution risk if ever reachable)
- Secrets (`.env`, SSH private keys) are excluded from both `.gitignore` *and* `.dockerignore` — the second matters separately, since `.gitignore` only protects Git history, not what `COPY . .` bakes into an image layer
- No Elastic IP — the instance is accessed exclusively over Tailscale, which maintains stable overlay addressing across stop/start cycles regardless of AWS-assigned public IP changes
- EC2 security group has no public inbound rules beyond Tailscale's UDP port — SSH and the app itself are not publicly reachable at all
- CSRF protection (e.g. Flask-WTF) is not implemented — a deliberate, documented scope call for a single-user tool behind login, not an oversight

## Database migration — on-prem to cloud

Data was migrated from `docker-server`'s Postgres container to EC2 using `pg_dump`/`psql` — the standard approach for any Postgres migration regardless of scale:

```bash
# dump data only (schema already exists on EC2 via db.create_all())
docker compose exec db pg_dump -U homelab -d homelab_portal --data-only > homelab_portal_data.sql

# move it directly between machines over Tailscale
scp homelab_portal_data.sql ubuntu@<ec2-tailscale-ip>:~/

# clear EC2's test data cleanly, resetting ID sequences
docker compose exec db psql -U homelab -d homelab_portal \
  -c "TRUNCATE TABLE service, server RESTART IDENTITY CASCADE;"

# restore (-T disables pseudo-terminal allocation required when piping stdin)
docker compose exec -T db psql -U homelab -d homelab_portal < ~/homelab_portal_data.sql
```

`--data-only` avoids schema collisions. `RESTART IDENTITY CASCADE` resets auto-increment counters before the restore so IDs land cleanly without conflicts. `-T` on the restore is easy to miss and silently breaks the import without it.

## Bugs caught by the test stage

Real issues the automated test environment caught before they could reach production, the point of having a real test stage:

**Gunicorn multi-worker race condition**: `db.create_all()` ran at module import time, meaning every gunicorn worker executed it independently on startup. Against a genuinely empty database (as the Test stage always creates), two workers raced to create the same table simultaneously and one crashed on a duplicate-key constraint. Fixed with `preload_app = True` in `gunicorn.conf.py` so initialization runs once in the master process before forking, plus a `post_fork` hook wrapping `db.engine.dispose()` in an explicit `app.app_context()` to give each worker its own fresh DB connection (inherited connections aren't safe to share across forked processes).

## Other incidents worth documenting

**Compromised deploy key**: during CI/CD debugging, a private deploy key was pasted into a third-party chat window while troubleshooting — caught immediately, treated as compromised regardless of platform retention policy. Rotated: old public key removed from `authorized_keys`, new key generated, Jenkins credential updated in place. The forced-command restriction meant actual exposure was limited (the key could only trigger a redeploy), but "limited blast radius" isn't the same as "acceptable to leave in place."

**Docker Compose `$` interpolation**: scrypt password hashes contain `$` delimiters that Compose treats as variable references. Fixed by escaping them as `$$` in `.env`. Easy to miss silently — the variable is swallowed rather than throwing an error.

**Forced-command SSH readiness probe**: the Jenkins Deploy stage originally probed EC2 SSH reachability by running `echo ready`. This succeeded at the network and authentication layer but was then intercepted by the forced-command wrapper, which rejected it as an unrecognized command and returned exit code 1 — making Jenkins incorrectly conclude the host wasn't ready yet. Fixed by adding `ready` as an explicit allowlisted command in the wrapper script and changing the probe to match. The forced-command restriction was working exactly as designed throughout; the probe just needed to respect it.

## What's next

- Proxmox backup status pulled from the existing API integration
- A second Jenkins build agent, so the controller itself never touches Docker directly — the real-company-scale version of the tradeoff documented above
- Push-based GitHub webhook via a tunnel service (e.g. `smee.io`), replacing Poll SCM's up-to-5-minute delay
- Alembic for schema migrations — `db.create_all()` only works cleanly against an empty database; any future schema change against existing data needs proper migration tooling
