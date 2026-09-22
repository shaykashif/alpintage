# Deploying to an Oracle Cloud instance

Everything here is paper trading and detection only -- nothing places a real
order, on Kalshi or anywhere else. Run these commands yourself over SSH; I
(the assistant) have no access to your Oracle account or instance.

## 1. SSH in and clone the repo

```bash
ssh opc@<your-instance-public-ip>          # Oracle Linux default user
# ssh ubuntu@<your-instance-public-ip>     # Ubuntu default user, if that's what you picked
git clone https://github.com/shaykashif/alpintage.git
cd alpintage
```

## 2. Run the setup script

```bash
bash deploy/setup.sh
```

It detects Oracle Linux (`dnf` + `firewalld`) vs Ubuntu (`apt` + `ufw`)
automatically. It installs `uv`, syncs the Python environment, walks you
through creating `.env` (you'll need a `TYPESAFE_API_KEY` and an
`ODDS_API_KEY`), installs two systemd services, and opens port 8080 in the
instance's own firewall. `uv` and this project both run fine on Ampere/ARM
(aarch64) -- no special steps needed for the Always Free Ampere shape.

## 3. Open the port in Oracle Cloud's console (the part the script can't do)

Oracle Cloud has a second, separate firewall at the cloud level, in front of
the instance's own one. The setup script cannot touch this -- you have to do
it in the OCI console:

1. Console -> **Networking** -> **Virtual Cloud Networks** -> your VCN ->
   your subnet -> the subnet's **Security List** (or, if the instance uses
   one, its **Network Security Group** instead).
2. **Add Ingress Rule**:
   - Source CIDR: `0.0.0.0/0` (anyone -- you chose to leave the dashboard
     open; use your own IP/32 instead if you'd rather restrict it later)
   - IP Protocol: TCP
   - Destination Port Range: `8080`
3. Save.

Until this rule exists, the dashboard won't be reachable from outside the
instance even though the service is running -- if `curl localhost:8080`
works on the box but the browser can't reach `http://<ip>:8080`, this is
almost always why.

## 4. Check it worked

```bash
systemctl status kalshi-loop.service
systemctl status kalshi-dashboard.service
journalctl -u kalshi-loop.service -f      # live log of the scan loop
```

Then visit `http://<your-instance-public-ip>:8080` in a browser.

## What's actually running

- **`kalshi-loop.service`** -- `scripts/run_loop.py`, every 30 minutes: runs
  the ladder/bracket scanner (paper-trades any violation found, which is
  rare by design), collects a batch of real Jev predictions, and scores
  anything settled. All paper. Logs to `data/*.jsonl` and `data/loop.log`.
- **`kalshi-dashboard.service`** -- `scripts/dashboard_server.py`, serving
  the panel at port 8080. Reads the same log files; never calls a live API
  itself, so it stays fast regardless of how often you refresh it.

## Updating after a code change

```bash
cd ~/alpintage
git pull
uv sync
sudo systemctl restart kalshi-loop.service kalshi-dashboard.service
```

## Turning it off

```bash
sudo systemctl stop kalshi-loop.service kalshi-dashboard.service
sudo systemctl disable kalshi-loop.service kalshi-dashboard.service
```

## Notes

- The dashboard has **no password** (your choice) -- anyone with the IP and
  port can view it. No secrets or real money are exposed by this data, but
  it is your activity on display. Revisit this if that changes.
- `.env` never gets committed (it's gitignored) and stays only on the
  server -- don't paste its contents anywhere, including back to me.
- Free-tier Oracle instances are small; this project is lightweight enough
  to run comfortably on one, but if the loop's Odds API or Polymarket calls
  ever start timing out, check `journalctl -u kalshi-loop.service` first.
- Oracle Linux ships SELinux in enforcing mode, and deploying under `$HOME`
  (as these instructions do) hits it twice -- both confirmed live, both
  fixed automatically by `setup.sh` now:
  1. **`.env` unreadable**: a file under `$HOME` is labeled `user_home_t`,
     which systemd's domain can't read even running as root (root bypasses
     Unix permissions, not SELinux). Both services fail to start with
     "Failed to load environment files: Permission denied".
  2. **Python interpreter unexecutable**: `.venv/bin/python` is a symlink
     uv creates pointing at its own managed Python build under
     `~/.local/share/uv/python/...` -- also `$HOME`, also `user_home_t`.
     SELinux checks the symlink's *target* for execute permission, so this
     is a separate denial from #1. Fails with `status=203/EXEC`.

  If you ever see either error, `sudo ausearch -m avc -ts recent` shows
  the denial. Manual fix if you're not re-running `setup.sh`:
  ```bash
  sudo semanage fcontext -a -t etc_t "$HOME/alpintage/.env"
  sudo restorecon -v "$HOME/alpintage/.env"
  sudo semanage fcontext -a -t bin_t "$HOME/.local/share/uv/python(/.*)?"
  sudo restorecon -R -v "$HOME/.local/share/uv/python"
  sudo semanage fcontext -a -t bin_t "$HOME/alpintage/.venv/bin(/.*)?"
  sudo restorecon -R -v "$HOME/alpintage/.venv"
  sudo systemctl restart kalshi-loop.service kalshi-dashboard.service
  ```
