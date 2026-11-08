# Workshop Desk

Workshop Desk provides separate garage workspaces, a public booking page for each garage, and an Ivory Digital superadmin dashboard. Garage records and accounts are stored in SQLite on TrueNAS.

## Upgrade the TrueNAS installation

Run the following as root in the TrueNAS shell. The app and client-data datasets must already exist. The dedicated database directory belongs to the container's UID and GID 1000.

```sh
/bin/bash -e <<'WORKSHOP_SETUP'
app=/mnt/apps/garageappprog
data=/mnt/photographers_data/garageappclientdata
source=https://raw.githubusercontent.com/valianktni1/servicesapp/main
test -d "$app"
test -d "$data"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
for file in Dockerfile server.py account.html index.html .dockerignore; do
  curl -fLsS --connect-timeout 10 --max-time 60 "$source/$file" -o "$tmp/$file"
done
install -d -m 0700 -o 1000 -g 1000 "$data/workshop"
for file in Dockerfile server.py account.html index.html .dockerignore; do
  install -m 0644 "$tmp/$file" "$app/$file"
done
echo "Workshop Desk files installed."
WORKSHOP_SETUP
```

Replace the Compose definition of the existing `servicesapp` stack with [serviceapp.yaml](serviceapp.yaml), then deploy in Dockge. It builds the backend locally. The reverse proxy continues to use port `30090` and serves `https://servicesapp.ivorydigital.uk`. Use `http://<TrueNAS-LAN-IP>:30090/health` to check the container and the HTTPS address for sign-in.

If Dockge does not rebuild after you update app files, open the TrueNAS shell in the host stack directory and run `docker compose -p servicesapp up -d --build`.

## Set the superadmin password

After deploying, run the following as root. It asks for a password interactively, so the password stays out of the public repository and your shell history.

```sh
docker exec -it "$(docker ps -q --filter label=com.docker.compose.project=servicesapp --filter label=com.docker.compose.service=servicesapp)" python server.py set-admin-password
```

Sign in at [the superadmin dashboard](https://servicesapp.ivorydigital.uk/admin) as `admin@ivorydigital.uk`. Running the password command again resets the password and revokes existing admin sessions.

## Garage access

Create a garage and copy its seven-day, single-use setup link to send to the owner. The owner enters their email and a password of 8 to 128 characters with at least one special character. A trial starts at setup and runs for 14 days unless extended. A complimentary account can run indefinitely or until a specified date. Paid access uses the paid-through date on manually recorded payments. Manual suspension takes precedence over a trial, complimentary access or payment, and the garage's records are retained.

The garage owner signs in at the homepage. The public booking page is `/book/<garage-code>`. Staff can copy an estimate approval link or open a draft in their email app. Reminders can also be sent from drafts and marked as sent. SMTP settings are saved for reference. Direct SMTP delivery, scheduled emails and online payment processing require further integration.

The old demo stored sample records in the browser. New garage workspaces start empty. The server database lives in the client-data dataset under `workshop/`.
