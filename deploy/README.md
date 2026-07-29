# Production deployment (DuckDNS + Caddy + VPS)

Replaces the Cloudflare quick tunnel with a stable HTTPS endpoint Meta can
deliver to unattended.

Everything here is compose configuration and ops scripting — no application
code changes.

---

## 1. Point a DuckDNS subdomain at the VPS

1. Sign in at <https://www.duckdns.org> and create a subdomain, e.g.
   `catchment-david`.
2. Set its IP to the VPS's **public IPv4 address**.
3. Confirm it resolves before going further:

   ```bash
   dig +short catchment-david.duckdns.org
   ```

**Do this first.** Caddy requests a certificate on its first start, and
Let's Encrypt validates by connecting to the domain over port 80. If DNS is not
yet pointing at the VPS the request fails, and repeated failures count against
Let's Encrypt's rate limits (5 failed validations per account, per hostname,
per hour).

If your provider gives the VPS a dynamic IP, you also need something to keep
the DuckDNS record current — see *Decisions left to you* below.

## 2. Provision the VPS (once)

```bash
ssh root@<vps-ip>
git clone <your-repo> catchment && cd catchment
sudo ./deploy/provision.sh
```

Installs Docker and the Compose plugin, restricts the firewall to 22/80/443,
and creates an unprivileged `deploy` user in the `docker` group. It is
idempotent — re-running it is safe and skips anything already done.

> **ufw does not filter Docker-published ports.** Docker inserts its rules
> ahead of ufw's filter chain, so a published port is reachable from the
> internet even when `ufw status` shows it denied. What actually keeps Postgres
> off the internet is `docker-compose.prod.yml`, which publishes nothing except
> Caddy's 80 and 443. **Always deploy with both compose files** — the base file
> alone publishes Postgres, Redis, the embedder and Langfuse.

Log out and back in as `deploy` (group membership only applies to new
sessions).

## 3. Configure

```bash
cp .env.example .env                        # application settings
cp .env.production.example .env.production  # DUCKDNS_DOMAIN, ACME_EMAIL
```

Fill both in. Neither is ever committed — both are gitignored.

`.env` needs at minimum a Groq API key, the WhatsApp app secret and verify
token, and a Langfuse key pair (see the root README). `.env.production` needs
only the domain and an ACME contact address.

## 4. Start

```bash
docker compose --env-file .env --env-file .env.production \
  -f docker-compose.yml -f docker-compose.prod.yml up -d
```

Both `--env-file` flags are required. Passing any `--env-file` disables
Compose's automatic loading of `.env`, so omitting the first one silently drops
every application setting.

Migrations run to completion before the API starts, as in local development.

Watch the certificate being issued:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml logs -f caddy
curl -sS https://<your-subdomain>.duckdns.org/health
```

`/health` returning JSON over HTTPS means TLS and the proxy are both working.

## 5. Point Meta at the new URL

In the Meta app dashboard → **WhatsApp → Configuration → Webhook → Edit**:

| Field | Value |
| --- | --- |
| Callback URL | `https://<your-subdomain>.duckdns.org/webhook/whatsapp` |
| Verify token | the value of `CATCHMENT_WHATSAPP_VERIFY_TOKEN` in `.env` |

Click **Verify and save**. Meta issues a `GET` handshake; a failure here is
almost always a verify-token mismatch or DNS not yet propagated.

Then confirm `messages` is still subscribed under **Webhook fields**, and send
yourself a message. Check it arrived:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml \
  exec -T postgres psql -U catchment -d catchment \
  -c "SELECT source_id, ingested_at FROM items ORDER BY ingested_at DESC LIMIT 5;"
```

> If the WABA subscription was previously repaired through the Graph API, that
> binding is to the *app*, not the URL — changing the callback URL does not
> undo it. No re-subscription needed.

## 6. Decommission the quick tunnel

The Cloudflare quick tunnel (`cloudflared tunnel --url ...`) is **development
only**: its hostname is random and changes on every restart, so Meta delivery
breaks whenever it stops.

Once a real message has arrived over the DuckDNS URL, stop it. Leaving it
running means two public entry points to the same webhook, one of which nobody
is watching.

---

## Operating notes

**Certificates persist.** `caddy_data` is a named volume holding the ACME
account key and issued certificates. `docker compose down` keeps it; `down -v`
destroys it and forces re-issuance, which is rate-limited to 5 duplicate
certificates per week. Prefer `down` without `-v`.

**Renewal is automatic.** Caddy renews at roughly two-thirds of the
certificate's lifetime. There is no cron job to add.

**Port 80 must stay open** even though all traffic is HTTPS — the ACME HTTP
challenge uses it, so closing it breaks renewal about 60 days later, long after
anyone would connect the two events.

**Query strings are not logged.** Caddy's access log redacts them, because
Meta's subscription handshake carries `hub.verify_token` there — the same leak
already fixed in the application's Uvicorn logger.

---

## Decisions left to you

- **Dynamic VPS IP.** If your provider does not give a static IPv4, the DuckDNS
  record will go stale after a reboot and Meta delivery stops silently. A small
  updater container hitting DuckDNS's update URL on a timer fixes it. Not
  included, because most VPS plans have static IPs and adding an unnecessary
  service that holds a DuckDNS token is worse than not having it.
- **SSH hardening.** `provision.sh` opens 22 and copies root's authorised keys
  to the deploy user, but does not disable password authentication or root
  login. Those lock you out if the key copy did not work, so I would rather you
  confirm key-based login works and then make that change deliberately.
- **Rate limiting / fail2ban.** Nothing beyond the port baseline. The webhook
  is HMAC-verified so forged requests are rejected, but there is no protection
  against volumetric abuse of the endpoint.
- **Backups.** The `pgdata` volume holds everything ingested. No backup is
  configured.
- **Non-root containers.** The app image already runs as an unprivileged user;
  `caddy:latest` runs as root, which is upstream's default and needed to bind
  80/443.
