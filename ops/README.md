# Production deployment

The production stack runs only `api` and `worker`; do not start the polling `bot`
service with the same Telegram token.

1. Point `PUBLIC_DOMAIN` DNS records to the VPS.
2. Copy `.env.production.example` to `.env.production` and replace every example
   value.
3. Create the files documented in `secrets/README.md`, then set directory mode
   `700` and file mode `600`.
4. Allow inbound TCP 22, 80 and 443 plus UDP 443 in the VPS firewall. Do not expose
   PostgreSQL or port 8000.
5. Validate and start:

   ```bash
   docker compose --env-file .env.production -f compose.production.yml config --quiet
   docker compose --env-file .env.production -f compose.production.yml up -d --build
   docker compose --env-file .env.production -f compose.production.yml ps
   ```

6. Inspect `https://$PUBLIC_DOMAIN/health/ready`. Prometheus is bound to localhost
   and can be viewed through an SSH tunnel to port 9090.
7. Store `/var/backups/neurokolo` on a VPS volume covered by provider snapshots or
   synchronize it to a separate encrypted/off-site destination.

The backup container creates a custom-format PostgreSQL dump daily and restores it
into a temporary database before marking the backup successful.
