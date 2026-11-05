# Installing on a Raspberry Pi or a server

This sets up one stack. Repeat the stack steps with another name and port for a second, such as staging next to production. A Pi 4 or 5 with 4 GB of memory runs both. Use 64-bit Raspberry Pi OS, and preferably a USB SSD, because SD cards wear out under database writes.

## 1. The machine

```bash
curl -fsSL https://get.docker.com | sh
sudo useradd --create-home --groups docker selltrack
sudo mkdir -p /opt/selltrack
sudo chown selltrack: /opt/selltrack
```

## 2. Access to the images

Images are in GitHub's container registry. The repository is private, so the machine needs a token that can only read them: a [classic personal access token](https://github.com/settings/tokens) with just `read:packages`.

```bash
sudo -u selltrack -H docker login ghcr.io --username <your GitHub username>
```

## 3. The deploy script and timer

Copy these from the repository:

```bash
sudo install -o selltrack -m 755 deploy/pi-deploy.sh /opt/selltrack/pi-deploy.sh
sudo cp deploy/systemd/selltrack-deploy@.service deploy/systemd/selltrack-deploy@.timer /etc/systemd/system/
sudo systemctl daemon-reload
```

## 4. The stack

```bash
sudo -u selltrack -H sh -c 'mkdir -p /opt/selltrack/prod && cd /opt/selltrack/prod && touch .env && chmod 600 .env'
sudoedit /opt/selltrack/prod/.env
```

Start from [`deploy/stack.env.example`](../../deploy/stack.env.example). Generate each password and key with `openssl rand -hex 32`, and **keep a copy somewhere safe**: without `SECRET_KEY` everyone is logged out, and without the database passwords a restored backup is harder to use. Set `BACKUP_UID` to the output of `id -u selltrack`.

For a staging stack, use `IMAGE_CHANNEL=staging`, `APP_ENV=staging` and a different `HTTP_PORT`.

## 5. Start it

```bash
sudo systemctl enable --now selltrack-deploy@prod.timer
journalctl -fu selltrack-deploy@prod
```

Within two minutes it deploys whatever `:prod` points at, so [promote a release](deploy.md#promoting-to-production) first. Then [create the first admin](../admin-guide.md).

## 6. Public HTTPS

Nginx only listens on the machine itself, so nothing is exposed yet. Point a tunnel at `http://localhost:8080`, which also avoids port forwarding:

- **Cloudflare Tunnel** is free and needs a domain.
- **Tailscale Funnel** is free and gives you a `*.ts.net` address.

Both add one more proxy in front of Nginx, which is why `TRUSTED_PROXY_COUNT=2` is the default. If it's wrong, logins can look like they all come from one address, or people can fake theirs.

Production cookies only work over HTTPS, so you can't log in until the tunnel is up.

## 7. Check it

From outside your network, check the login page loads and has a padlock. Then, on the machine:

```bash
journalctl -u selltrack-deploy@prod | tail
```

Backups start by themselves. Check **System** in the admin console the next day. Backups live on the same machine as the database, so a dead SD card or SSD takes both. Copy them somewhere else regularly.
