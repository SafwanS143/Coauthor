# Deploying coauthor to EC2

End state: a dashboard at `https://coauthor.<ip>.sslip.io`, behind a shared
password, refreshing itself every ten seconds, that anyone in the group can open
on a phone without installing anything.

Roughly 45 minutes, most of it waiting for `apt`.

---

## What it costs

Verified against AWS list pricing on 2026-07-27, `us-east-1`, excluding tax.

| item | rate | monthly |
|---|---|---|
| t4g.small on-demand, 24/7 | $0.0168/hr × 730 | $12.26 |
| gp3 root volume, 12 GB | $0.08/GB-month | $0.96 |
| Elastic IP (public IPv4) | $0.005/hr × 730 | $3.65 |
| S3 deployment bucket | a few MB stored | under $0.01 |
| data transfer out | first 100 GB/mo free | $0.00 |
| | | **~$16.87/mo** |

`terraform output estimated_monthly_cost` recomputes this from whatever
instance type and volume size you actually applied. It counts the three charges
that matter and ignores the sub-cent S3 line.

Two things worth knowing before you start:

- **Nothing stops itself.** The instance runs until you `terraform destroy` it.
  Set a zero-spend budget alarm in AWS Billing first.
- **A `.micro` will not work.** Chromium with a Docs editor open sits at
  600–900 MB resident and gets OOM-killed on 1 GB, reliably at an inconvenient
  hour. The Terraform refuses anything smaller than `.small`.

---

## Before you start

You need the AWS CLI authenticated (`aws sts get-caller-identity` should
return your account), Terraform installed, and a VNC client for one step near
the end — RealVNC Viewer or TightVNC are both fine.

---

## 1. Service account for the poller

This removes the 7-day OAuth token expiry. Without it you would have to VNC into
the instance every week to click through a consent screen.

1. Google Cloud console → your project → **IAM & Admin → Service Accounts** →
   **Create service account**. Name it `coauthor-poller`. No roles needed —
   project roles are irrelevant here, access comes from Drive sharing.
2. Open it → **Keys** → **Add key** → **Create new key** → **JSON**. It
   downloads once.
3. Save that file as `service_account.json` in the project root.
4. Copy the account's email — it looks like
   `coauthor-poller@yourproject.iam.gserviceaccount.com`.
5. **Share the Google Doc with that email as an Editor**, exactly like sharing
   with a person. This is the step that grants access; skipping it produces a
   404 from the Drive API that looks like a code bug.

Then point the config at it:

```json
"service_account_path": "service_account.json"
```

Verify locally before deploying:

```powershell
python activity_poller.py --once
```

It should print a `revisions=` count with no browser window opening. If a
browser opens, `service_account_path` is still null.

---

## 2. About the browser profile

**A Chrome profile created on Windows will not work on the instance.** Chrome
encrypts its cookies at rest with a key stored in `Local State`, and on Windows
that key is sealed with DPAPI, tied to your Windows user account. Linux
Chromium cannot unseal it, so the cookies arrive intact and unreadable, and the
watcher lands on a plain sign-in page rather than the document.

There is no way around this by copying files. The profile has to be created on
the instance, which is step 6. `-PushProfile` still matters for
`service_account.json`, so run it regardless.

Copying a profile *does* work Linux-to-Linux — from a Raspberry Pi, another EC2
box, or WSL — if you happen to have one of those already set up.

> `chrome-profile/` contains live Google session cookies. It is equivalent to a
> password. It is in `.gitignore`, it is uploaded with server-side encryption,
> and it lands on an encrypted volume. Keep it that way.

---

## 3. Build the infrastructure

```powershell
cd terraform
terraform init
terraform apply
```

Review the plan and type `yes`. About two minutes.

```powershell
terraform output
```

Note `dashboard_url`, `instance_id` and `deploy_bucket`. The instance is now
installing packages in the background — Caddy, Chromium's dependencies, Python.
Give it about five minutes before the next step.

What this created, and why it is shaped this way:

- **No SSH.** No key pair exists and port 22 is closed. Administration is
  through SSM Session Manager, which needs no inbound rule at all.
- **Only 80 and 443 open.** 80 exists solely so Let's Encrypt can complete its
  HTTP-01 challenge; everything on it redirects.
- **Encrypted root volume**, because the browser profile lives on it.
- **An Elastic IP**, so the hostname inside the TLS certificate survives a
  stop/start.
- **IMDSv2 required**, so a request-forgery bug in the web app cannot read
  instance credentials.

---

## 4. Deploy the code

```powershell
cd ..
.\deploy.ps1 -PushProfile
```

`-PushProfile` also uploads `chrome-profile/`, `service_account.json` and any
OAuth files. Use it the first time and after any re-login; plain `.\deploy.ps1`
afterwards, which sends only code.

The script packages the app, uploads it to the private S3 bucket, and triggers
the redeploy over SSM. It streams the output back and finishes by printing your
dashboard URL and the generated password.

The secrets tarball is deleted from S3 immediately after it is unpacked.

---

## 5. Open it

Go to the `dashboard_url`. The password was generated on the box; read it any
time with:

```powershell
aws ssm start-session --target <instance-id> --region us-east-1
sudo cat /etc/coauthor.env
```

To change it:

```bash
sudo nano /etc/coauthor.env       # edit COAUTHOR_PASSWORD
sudo systemctl restart coauthor-web
```

Leave `COAUTHOR_SECRET` alone unless you want to sign everyone out.

The certificate takes 10–30 seconds on first load while Caddy talks to
Let's Encrypt. A browser warning after a minute means the ACME challenge
failed — check `sudo journalctl -u caddy -n 50`.

---

## 6. Sign the watcher in, on the instance

This step is required, not a fallback — see step 2. The dashboard will show
**Watcher offline** until it is done, while edit history collects normally.

You need two things installed locally, once:

- [Session Manager plugin](https://s3.amazonaws.com/session-manager-downloads/plugin/latest/windows/SessionManagerPluginSetup.exe)
  — `aws ssm start-session` does not work without it. Open a new terminal after
  installing so it is on PATH.
- Any VNC viewer, e.g. [TightVNC](https://www.tightvnc.com/download.php)
  (viewer only) or [RealVNC Viewer](https://www.realvnc.com/en/connect/download/viewer/).

**Terminal 1 — the tunnel.** Leave it running. Shorthand parameters avoid
PowerShell mangling the JSON form:

```powershell
aws ssm start-session --target <instance-id> --region us-east-1 `
  --document-name AWS-StartPortForwardingSession `
  --parameters "portNumber=5900,localPortNumber=5901"
```

The local end is **5901** on purpose. TightVNC's standard installer includes a
server as well as the viewer and leaves it listening on 5900, which shadows a
tunnel bound to the same port: the viewer then reaches that local server and
answers `Sorry, loopback connections are not enabled`, which reads like a
problem on the instance even though nothing ever reached it. Check with
`Get-NetTCPConnection -State Listen -LocalPort 5900` if in doubt.

**Terminal 2 — start the browser.**

```powershell
aws ssm start-session --target <instance-id> --region us-east-1
```

then, inside that session:

```bash
sudo systemctl stop coauthor-watcher
pgrep -f "Xvfb :99" || sudo nohup Xvfb :99 -screen 0 1680x1000x24 >/tmp/xvfb.log 2>&1 &
pgrep -f x11vnc     || sudo nohup x11vnc -display :99 -localhost -nopw -forever -shared >/tmp/x11vnc.log 2>&1 &
cd /opt/coauthor/app
sudo -u coauthor env DISPLAY=:99 /opt/coauthor/venv/bin/python presence_watcher.py --login
```

`cd` first — `profile_dir` is a relative path, so the profile must be written
next to the application.

**Now connect the viewer to `localhost:5901`.** A Chromium window is sitting on
the Google sign-in page. Sign in as the tracking account and complete 2FA. The
sign-in happens from the instance's own IP, which is why it works here and
cannot be shipped from your laptop.

Back in terminal 2, press Enter once the doc renders, then Enter again to save
the profile. Then:

```bash
sudo pkill x11vnc
sudo systemctl start coauthor-watcher
sudo systemctl status coauthor-watcher --no-pager
```

The dashboard should flip to **Live** within a minute or so.

`x11vnc` binds to localhost and is reachable only through the tunnel, but kill
it when you are done regardless.

---

## Running it

```bash
sudo systemctl status coauthor-watcher coauthor-web
sudo journalctl -u coauthor-watcher -f
sudo systemctl list-timers coauthor-poll.timer
sudo coauthor-deploy                  # re-pull and restart
```

| service | does |
|---|---|
| `coauthor-watcher` | Chromium in the doc under xvfb. Restarts daily on purpose — Chromium leaks over long runs. |
| `coauthor-web` | the dashboard, bound to `127.0.0.1:8000`, reachable only through Caddy |
| `coauthor-poll.timer` | one poller pass every 5 minutes |
| `caddy` | TLS and reverse proxy |

The dashboard tells you when something is wrong: a **Watcher offline** banner
if no presence event has arrived recently, and a separate notice if the poller
has fallen behind. Both explain the consequence in plain language, because the
people reading it are not the people who built it.

---

## Watch for

- **Silent death.** Chromium can be OOM-killed and systemd will restart Python
  into a browser that never loads. The offline banner is your alarm — if it is
  up for hours, restart the watcher.
- **Session expiry.** Unattended Google sessions last a long time but not
  forever. The fix is step 6 again.
- **Selector drift.** Google's minified class names churn. If presence stops
  resolving names while the watcher is plainly running, run `--discover` and add
  a selector to `extra_selectors`. Edit data is unaffected.

---

## Turning it off

```powershell
cd terraform
terraform destroy
```

Everything goes, including the S3 bucket and the Elastic IP — an unassociated
Elastic IP still bills at $0.005/hr, so do not leave one behind.

**Copy the database off first if you want to keep it:**

```bash
# in an SSM shell, before destroying
sudo aws s3 cp /opt/coauthor/app/coauthor.sqlite3 s3://<deploy-bucket>/backup.sqlite3
```

```powershell
aws s3 cp s3://<deploy-bucket>/backup.sqlite3 .\coauthor.sqlite3
```
