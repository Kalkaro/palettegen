# security and deployment

The server is locked to `127.0.0.1` by default. Keep that binding when placing
Caddy or another HTTPS reverse proxy in front of it; do not expose port 8765
through the VM firewall.

For a public deployment, set `PALETTE_PUBLIC=1` and provide
`PALETTE_PASSWORD` with at least 16 characters. The browser will use HTTP Basic
authentication. Store the environment file with mode `600`, and never commit
it.

The application enforces:

- authentication in public mode;
- same-origin generation requests and an optional generation rate limit;
- a single active Stylix process with a four-minute timeout;
- HTTPS and host allowlisting for remote wallpaper downloads;
- DNS-pinned public-HTTPS validation for user-supplied image URLs, including
  private, loopback, link-local, and reserved address blocking on every
  redirect;
- bounded API responses, image downloads, dimensions, history count, and disk
  use;
- JPEG, PNG, and WebP decoding and signature validation before processing;
- atomic metadata writes, strict record IDs, and non-symlink file serving;
- palette key/value validation;
- CSP, anti-framing, MIME-sniffing, referrer, and browser permission headers;
- a pinned Stylix revision;
- generic client errors while retaining detailed server-side logs.

Production checklist:

1. Run as a dedicated unprivileged service account.
2. Install `nix-portable` outside `/tmp`, owned by that account and mode `700`.
3. Put the application behind Caddy with automatic HTTPS.
4. Allow inbound ports 80 and 443 only; keep 8765 private.
5. Back up `palette-history` if the history matters.
6. Enable VM security updates and log rotation.
7. Rotate the password if it may have been exposed.

Example startup:

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
set -a
. ./.env
set +a
exec .venv/bin/python palette_server.py
```

Use a random password, for example:

```sh
openssl rand -base64 32
```

Example Caddy configuration:

```caddyfile
palette.example.com {
    encode zstd gzip
    reverse_proxy 127.0.0.1:8765 {
        header_up X-Real-IP {remote_host}
    }
}
```

This substantially reduces the application-level attack surface, but it is not
a substitute for patching the VM, protecting the cloud account, and monitoring
resource usage.
