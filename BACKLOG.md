# Backlog: account security and Platform settings (port from MCP-Control-Platform)

> **Status 2026-10-02: all 4 phases built and deployed (`857d825`).** What's left is in CLAUDE.md "Open items"
> (office allowlist, the user's open questions, and the next idea: the 3D God's Eye View globe, item 14).

**Next session: read `CLAUDE.md` first, then this file. It's everything needed to build this without
re-reading the other project.** The user decided the scope on 2026-10-01: build **everything** below, plus a
**Platform settings** page for TWG to configure it all. Tick items off here as they ship; delete this file when done.

## Source
- Repo **`TWG-Security/MCP-Control-Platform`**, commit **`b17a07a`** (2026-08-25, the `master` head).
  - A local copy is at **`~/src/MCP-Control-Platform`**, checked out at b17a07a.
  - Re-clone if it's gone: `git clone https://github.com/TWG-Security/MCP-Control-Platform.git ~/src/MCP-Control-Platform && git -C ~/src/MCP-Control-Platform checkout b17a07a`.
    The PAT in `~/.git-credentials` has access.
- The source is **TypeScript** (Fastify, Drizzle, React). The portal is **Python** (FastAPI, SQLAlchemy, Jinja, vanilla JS).
  **Port the designs and logic; don't copy files.** Read the source file listed for each item when you implement it.
- **SNMP is not in that repo** (no branch has it), nor in any `TWG-Security` repo. **Ask the user** where it is and what it's for. The likely fit is SNMP traps → portal alarms.

| Area | Source files (in `~/src/MCP-Control-Platform`) |
|---|---|
| Ban, lockout, allowlist, client IP | `apps/api/src/security/intrusion.ts` |
| Cloudflare edge bans | `apps/api/src/security/cloudflare-edge.ts`, `apps/api/test/cloudflare-edge.test.ts` |
| Security API | `apps/api/src/routes/security.ts` |
| Login stages (password → TOTP / recovery / passkey / enroll), forgot/reset, passkey login | `apps/api/src/routes/auth.ts` |
| TOTP, recovery codes, WebAuthn | `apps/api/src/mfa/{totp,recovery,webauthn}.ts`, `packages/core/src/crypto.ts` (`generateRecoveryCodes`, `hashRecoveryCode`) |
| Self-service account (password, MFA, passkeys, sessions) | `apps/api/src/routes/me.ts` |
| Invites, setup link, reset 2FA (tenant users / operators) | `apps/api/src/routes/users.ts`, `apps/api/src/routes/operators.ts` |
| Sessions | `apps/api/src/session.ts`, `apps/api/src/session-cookie.ts` |
| Password policy and expiry | `packages/core/src/password-policy.ts`, `apps/api/src/password-expiry.ts` |
| Email | `apps/api/src/mail/{mailer,templates}.ts` |
| Google sign-in (OIDC) | `apps/api/src/routes/sso.ts` |
| Platform settings storage | `apps/api/src/platform-settings.ts` |
| Break-glass | `apps/api/src/routes/breakglass.ts` (we replace this with a CLI command) |
| Their own security notes | `docs/SECURITY.md` §1 |

## Rules that still apply (from CLAUDE.md)
- **Alarms must never be missed or delayed.** A security feature must never lock an operator out of the monitoring screen during an incident. See "Alarm safety" in each phase.
- **Multi-company isolation:** every query goes through `Scope.where(...)` and every write through `scope.require_write(...)`. Add every new endpoint to `tests/test_tenancy.py`.
- **Platform-only settings** need `platform.manage`. Per-company settings are edited by that company's admin, or by TWG in support mode.
- **Tell the user before redeploying.** Never print secrets. All secrets at rest use `app.security.encrypt` (Fernet, `FERNET_KEY`).
- **Email branding** (TWG org rule): white background, a **5px solid #1A1A1A border** around the header block, the logo enlarged and centered (`https://static.wixstatic.com/media/d4fc57_b28e915a1b924e33b12eb77028f74f62~mv2.png`), **#C0392B** dividers and heading accents, **#E08A30** buttons, Arial/Helvetica. **No dark-filled header bar.**

## Open questions (ask; build the rest meanwhile)
1. **SNMP**: where is the code, and what does it need to do? (See Source.)
2. **Mandatory 2FA scope.** Recommended: a platform toggle "Require 2FA for TWG" plus "Require 2FA for every company" (or let each company decide in its own Settings).
3. **SMTP account** for invites and resets, e.g. a Google Workspace mailbox and app password (`smtp.gmail.com:587`, STARTTLS).
4. **Cloudflare API token** (permission *Zone → Firewall Services → Edit*, scoped to `twgsecurity.net`) and the **Zone ID**. The user enters both on the Platform settings page, never in chat.
5. **TWG office public IP(s)** for the allowlist (day-one requirement; see phase 1).
6. **Tunnel connector IP**, for trusting `CF-Connecting-IP`. App logs showed tunnel requests from **`10.0.2.58`**: verify (e.g. a request through the domain while watching `docker compose logs app`).
7. **Google OAuth client** (ID and secret, redirect `https://alarmportal.twgsecurity.net/auth/google/callback`) for Google sign-in.

## Platform settings page (TWG only, `platform.manage`)
- **Where:** a new page `/platform` with a "Platform" nav item, shown to `platform.view` users; changes need `platform.manage`. Link it from the Companies page, since the user asked for it "in the All companies" area.
- **Storage:** a single-row table `platform_settings` (id=1) with explicit columns. Secrets go in `*_enc` columns (Fernet) and come back to the browser masked only. Every save writes the audit action `platform.settings` (field names only, never values) into the platform tenant's log.
- **Sections:**
  1. **Sign-in protection:**
     - ban rules: max failures, window, first ban, max ban, permanent after N bans, account lock threshold
     - IP allowlist (add/remove, with label)
     - active bans (unban; add a manual ban)
     - locked accounts (clear)
     - recent sign-in events (filter by IP, email, outcome)
     - "my IP" display
  2. **Cloudflare:** API token (masked), Zone ID, enable toggle, **Test** button (read-only list of rules), status (last OK / last error).
  3. **Two-factor:** require for TWG; require for all companies, or let each company decide; allow passkeys.
  4. **Passwords:** policy (min length, upper/lower/number/symbol), expiry days (0 = never).
  5. **Sessions:** lifetime (12 h today), idle timeout (**default OFF**; see Alarm safety in phase 2).
  6. **Email:** SMTP host, port, TLS mode, user, password (masked), From, enabled; **Send test email**.
  7. **Google sign-in:** client ID, secret (masked), enabled, optional allowed email domains.
- **Per-company settings** (that company's Settings page): "Require 2FA for our users" when the platform lets companies decide.

## Phase 1: sign-in protection + Cloudflare edge bans: **DONE** (commit after `0589f1d`, not deployed yet)
Built as specified, plus: an IP with a signed-in user active in the last 15 min is never banned (alarm safety);
an admin unban/unlock writes a `cleared` auth event so older failures stop counting; the connector is trusted from
the Platform page (or `app.cli trust-proxy`), and an untrusted connector sending `CF-Connecting-IP` is flagged there.
**On deploy:** `trust-proxy --ip 10.0.2.58` and allowlist the office IP (this server's public IP is `204.186.88.58`; confirm with the user).
Replaces the in-memory `_FAILS` brake in `app/routers/auth.py` (10 failures per IP per 15 min).
- **Tables** (migration 0008): `platform_settings`, `auth_events` (ts, ip, email, kind, outcome, reason, tenant_id null), `ip_bans` (ip unique, reason, fail_count, ban_count, expires_at, permanent, cf_rule_id), `ip_allowlist` (ip, label).
- **Client IP** (`app/net.py`):
  - Use `CF-Connecting-IP` **only** when the direct peer is a trusted proxy (`TRUSTED_PROXY_IPS`: the tunnel connector, and Caddy on the docker network); otherwise use the peer IP. Port `clientIp()` from intrusion.ts.
  - This also fixes the audit log showing the connector IP for every tunnel user: use it in `deps.client_ip`.
  - Tighten uvicorn `--forwarded-allow-ips='*'` (Dockerfile) to Caddy's address.
- **Ban logic** (port `recordAuthEvent`, `maybeBan`, `isAccountLocked`, `banIp`, `unban`, allowlist functions):
  - Defaults: **5** failures in **10 min** → ban **15 min**; each later ban (after the last one expired) lasts ×4, capped at **7 days (10080 min)**; ban #**5** is **permanent**.
  - Account lock: **10** failures for one email across all IPs within the window. It refuses before the password hash check and returns the generic "invalid" response; it doesn't record a new failure (so the lock can age out); it fails open on database errors.
  - Burn one dummy password hash for unknown emails, so timing doesn't reveal which accounts exist.
  - Record every sign-in kind: login, totp, recovery, passkey, reset, sso.
- **Cloudflare** (port `cloudflare-edge.ts`, using httpx with an 8 s timeout):
  - Create: `POST https://api.cloudflare.com/client/v4/zones/{zone}/firewall/access_rules/rules` with `{mode:"block", configuration:{target:"ip"|"ip6", value}, notes:"twg-alarm-portal auto-ban: <reason>"}`. Store the rule id. On a "duplicate" error, adopt the existing rule.
  - Remove on unban, allowlist and expiry, using the stored id. Fallback lookup: `GET …?configuration.value=<ip>`, matching **only our notes prefix**, so manual Cloudflare rules are never touched. "Not found" counts as clean.
  - A sweep (an asyncio loop like the arming scheduler, every 60 s) removes edge rules of expired bans.
  - **Best-effort only:** Cloudflare being slow or down never blocks, delays or undoes the local ban. Show the last error on the Platform page.
- **Banned page:** a banned IP gets a plain 403 page ("This address is temporarily blocked until …"). Check bans in middleware on `/login` and `/api/*` only (cheap indexed lookup); never on the SSE stream of a signed-in session.
- **CLI** (`app/cli.py`): `unban --ip`, `allow-ip --ip --label`, `clear-lock --email`.
- **Alarm safety:**
  - **Never ban or push to Cloudflare:** allowlisted IPs, the `TRUSTED_PROXY_IPS`, and private/loopback/link-local ranges (RFC 1918, 127/8, ::1, fc00::/7, fe80::/10). If IP resolution fails, the request looks like it comes from the connector (a private address), so it fails safe instead of banning everyone.
  - Put the TWG office IP on the allowlist on day one (question 5).
  - The LAN address `https://10.1.10.97` is never subject to bans.
  - Locked accounts are visible and clearable on the Platform page and by CLI.
- **Tests:**
  - unit tests: escalation (15m → 60m → 4h … → permanent), the account lock across IPs, the allowlist and private IPs never being banned, trusted-proxy header handling (a forged header from an untrusted peer is ignored)
  - Cloudflare calls mocked with respx: push, duplicate adoption, remove, expiry sweep, "down" leaves the local ban in place
  - `test_tenancy` additions for the new endpoints
  - an e2e run: 5 bad sign-ins → banned page, then unban from the Platform page

## Phase 2: two-factor, passkeys, sessions: **DONE** (not deployed yet)
Built as specified, with these choices: the between-stage state (`pending`) lives in the signed session cookie
instead of an itsdangerous URL ticket; passkey sign-in is usernameless (no email, so no "has a passkey"
oracle; challenge in the session); with 2FA required, a passkey-only user's password sign-in asks for the
passkey; "lifetime" is the **closed-browser** limit (12 h, as the cookie did before), plus optional max age and
idle sign-out (both off); a revoked screen stays on the page with a loud SIGNED OUT banner + tone (no redirect);
sessions from before are adopted (no sign-out on deploy); the live stream is woken by a bus event on revoke.
- **Tables** (migration 0009):
  - `users`: `totp_secret_enc`, `totp_enabled`, `last_totp_step`, `password_changed_at`, `token_version`
  - `mfa_recovery_codes` (user_id, code_hash, used_at)
  - `webauthn_credentials` (user_id, credential_id, public_key, counter, transports, name, created_at, last_used_at, tested_at)
  - `user_sessions` (id, user_id, created_at, last_seen_at, ip, user_agent, revoked_at)
- **TOTP** (`pyotp`): secret Fernet-encrypted; ±1 time step tolerance; **reject replay** (`step <= last_totp_step`); QR code via the `qrcode` package (PNG, data URL); issuer "TWG Alarm Portal".
- **Recovery codes:** 10 codes of 16 hex characters grouped `xxxx-xxxx-xxxx-xxxx`, stored as SHA-256, shown once, single-use, regenerate on demand.
- **Login stages** (port `auth.ts` 136–200 and 397–500):
  - password OK + 2FA on → a short-lived signed **MFA challenge ticket** (itsdangerous, purpose "mfa", 5 min) → `/login/totp` or `/login/recovery`
  - 2FA required but not enrolled → a scoped **enroll ticket** (purpose "enroll", 15 min) that only allows the TOTP setup/enable endpoints. On enable, show the recovery codes, then start the session (grace enrollment; nobody gets locked out).
- **Passkeys** (`webauthn`, Duo's py_webauthn; browser side via vendored `@simplewebauthn/browser` UMD in `static/vendor/`):
  - RP ID `alarmportal.twgsecurity.net`, origin `https://alarmportal.twgsecurity.net` (config `WEBAUTHN_RP_ID` / `WEBAUTHN_ORIGIN`). Hide passkey buttons when `location.hostname` is an IP; browsers refuse WebAuthn there.
  - Register / test / remove passkeys on an **Account** page. "Sign in with a passkey" counts as strong auth and skips TOTP.
  - Store the challenge server-side per user and clear it after use; update the counter on use.
  - **e2e:** Playwright CDP `WebAuthn.addVirtualAuthenticator`.
- **Sessions:**
  - The session cookie carries a `sid`; `current_user` checks that the `user_sessions` row isn't revoked (indexed lookup; update `last_seen_at` at most once a minute).
  - Account page: "Your sign-ins", revoke one, **sign out everywhere** (bumps `token_version`).
  - Admins see and revoke their company's users' sessions.
  - Password change or reset revokes the user's other sessions.
  - A revoked session's open live stream (SSE) is closed by publishing a bus event the stream handler listens for.
- **Admin:** "Reset 2FA" on the Users page clears TOTP and recovery codes, not passkeys (audited). **CLI** `reset-mfa --email` is the break-glass route.
- **Alarm safety:**
  - The **idle timeout defaults to OFF**: a wall-mounted monitoring screen must never sign itself out. If a company turns it on, warn on the setting that open alarm screens will sign out.
  - A session expiring or being revoked must **send the user to sign-in loudly**, not leave the page looking live. Today the 401 handling in `common.js api()` redirects, and the SSE reconnect gets 401. Verify the "LIVE UPDATES LOST" banner and tone fire before the redirect.
- **Tests:** TOTP enable and verify; replay rejected; recovery codes single-use; the enroll-ticket scope (it can't call other APIs); a passkey e2e with the virtual authenticator; session revoke kills access within one request; sign out everywhere.

## Phase 3: email, invites, forgot password, password rules: **DONE** (not deployed yet)
Built as specified, using stdlib `smtplib` in a thread (no aiosmtplib). Also: an `email_log` table shown on the
Platform page; the forgot-password work runs in the background (same answer and timing either way) with a
3-per-hour cap per account (serialised with a lock) and 10 per IP per 15 min; invited users who use "Forgot
password" get a fresh invite. Dev/e2e passwords changed to meet the rules (dev admin: `Smoke-test-password-123!`).
**Needs from the user:** an SMTP mailbox (open question 3); until then invites show a copyable link.
- **Email:** SMTP via `aiosmtplib` (or stdlib `smtplib` in `asyncio.to_thread`); 3 attempts with backoff; every send logged (to, subject, outcome; never the body).
  - Settings live in `platform_settings`, with the password encrypted. "Send test" button.
  - When sending is disabled: skip and log, and the UI shows the copyable link instead.
  - Templates (`app/templates/email/*.html` + plain text): branded per the rule above, using `brandedEmail()` in `mail/templates.ts` as the structure.
- **Invites:**
  - New users get status `invited` and no password. A **setup link** carries a random token, stored hashed (SHA-256), valid **7 days**, single-use.
  - `/setup?token=` greets them by email and company, and sets a password (policy-checked). Then 2FA enrollment if required (same enroll ticket), then the session.
  - Users page: **Invite**, **Resend**, **Copy setup link**.
  - **Companies → New company** switches to inviting the first admin by email (keep the temporary-password option as a fallback).
- **Forgot password:**
  - `/forgot` always answers "if that account exists, we've emailed it" (no enumeration).
  - The reset token is hashed, lasts **30 min**, single-use, and is enforced on submit. `GET /reset/check` shows "expired / used / invalid" on page load without consuming the token.
  - A reset bumps `token_version`, revoking all sessions.
- **Password policy:** **12+ characters with upper, lower, number and symbol** (configurable). Enforce it everywhere a password is set (create, change, reset, setup, CLI); the error lists what's missing. Port `validatePassword`. Replace the bare `min_length=12` checks in `api.py` and `cli.py`.
- **Password expiry** (optional, 0 = never): an expired password forces a change right after sign-in, passkey and SSO included (port `password-expiry.ts`).
- **Tests:** a local SMTP sink (`aiosmtpd`) catches the mails; follow the links. Tokens are single-use, expired tokens fail, no enumeration (same response and timing), policy messages.

## Phase 4: Google sign-in (OIDC): **DONE** (not deployed yet)
Built as specified, except: no URL-fragment completion ticket (that was for the source's SPA; the callback
starts the session itself from the signed session state); the ID token from the token endpoint is checked
(iss, aud, exp, nonce, email_verified) without a signature check, as OIDC allows for a direct TLS response;
starting on another host (the LAN IP) first moves to the portal address. **Needs from the user:** the Google
OAuth client (open question 7); they enter it on the Platform page.
Port `sso.ts`:
- authorization-code flow with `state` and PKCE, plus `iss` validation (the source has this as an open hardening item)
- **match existing accounts only** by Google-**verified** email; never auto-create accounts
- skips local TOTP (delegated); password expiry still applies
- the completion ticket goes in the URL **fragment** (60 s)
- client ID and secret in `platform_settings` (encrypted); "Sign in with Google" shows on the login page only when configured
- record `sso` events in `auth_events`
- if the user also puts Cloudflare Access in front, decide with them whether to keep both

## Not in scope (unless the user asks)
- Watched-account alerts (`user-audit.ts`), OAuth for MCP connectors, gateway tokens, container exec.
- **Google Drive off-server backups** (`apps/api/src/backup-drive.ts`): an encrypted bundle uploaded to Drive. This would close the "everything is on one server" backup gap in CLAUDE.md. Offer it.

## Python dependencies to add
`pyotp`, `qrcode` (Pillow is already present), `webauthn`, `aiosmtplib`. For tests: `aiosmtpd`.

## Where it lands in the portal
- `app/routers/auth.py` (login stages, setup, forgot/reset, passkey login, Google)
- new `app/routers/account.py` (Account page: password, 2FA, passkeys, sessions)
- new `app/routers/platform.py` (Platform settings + Security APIs)
- new `app/security_guard.py` (bans, lockout, Cloudflare)
- `app/net.py` (client IP)
- `app/mail.py` (email)
- `app/deps.py` (session check)
- `app/models.py` + migrations 0008–0010
- templates: `login.html` (stages), `setup.html`, `forgot.html`, `reset.html`, `account.html`, `platform.html`
- JS: `account.js`, `platform.js`, `login.js`
- update `CLAUDE.md` (architecture table, rules) and `CHANGELOG.md` as each phase ships
