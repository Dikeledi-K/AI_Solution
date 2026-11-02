# Running ClinicQ inside a clinic, without needing the internet

The public demo runs on Streamlit Community Cloud, so every device needs internet. A real clinic
should not depend on that: connections drop and load-shedding takes routers down. Instead, ClinicQ
runs on **one small computer inside the clinic**, and the reception and staff tablets connect to it
over the clinic's own Wi-Fi.

```
 Tablets (reception, vitals, consulting rooms, pharmacy)      Waiting-room TV
        │  https://clinicq.lan  (clinic Wi-Fi, WPA2/WPA3)          │
        └──────────────────────────┬────────────────────────────────┘
                                   ▼
                  Clinic server (mini-PC, on a UPS)
                  Caddy (HTTPS)  ──►  ClinicQ (127.0.0.1:8501)  ──►  SQLite database
                                   │                                  (BitLocker-encrypted disk)
                                   └── internet, when available: AI provider, SMS gateway
```

> ⚠️ This is a prototype. Before real patients, the clinic needs clinical sign-off, a POPIA review and
> an information officer – see [RESPONSIBLE_AI.md](../RESPONSIBLE_AI.md).

## What works without internet

| Works offline | Needs internet (and what happens without it) |
|---|---|
| Registration, folders, queue numbers | **AI translation & triage suggestion** – the red-flag rules still triage every patient, and the AI part falls back to **YELLOW – manual review**. Nothing crashes. |
| Red-flag danger-sign rules (5 languages) | **SMS** – patients watch the **waiting-room screen** instead. |
| Vitals, TEWS, colours, every station's queue | |
| Rooms, alerts, records, audit log, reports | |
| Waiting-room screen, encrypted backups | |

## 1. Hardware

- **Server:** any small PC with Windows 10/11 **Pro** (for BitLocker) or Linux, 8 GB RAM, SSD. Connect it
  to the router **with a cable** and give it a **fixed IP address** (e.g. `192.168.1.10`) in the router.
- **UPS** for the server **and** the Wi-Fi router, so load-shedding does not stop the clinic.
- **Tablets:** Android tablets (10") with Chrome. One per desk/room.
- **Waiting-room TV:** any smart TV or a TV with a cheap HDMI stick that runs a browser.
- **Wi-Fi:** a staff network with a strong WPA2/WPA3 password, separate from any guest Wi-Fi.

## 2. Encrypt the server's disk

Turn on **BitLocker** (Windows Pro: *Settings → Privacy & security → Device encryption / BitLocker*), or
LUKS on Linux. The database holds identifiable health information; if the PC is stolen, the data
stays unreadable. Store the recovery key with the facility manager, not on the PC.

## 3. Install ClinicQ

```powershell
cd C:\ClinicQ
git clone https://github.com/Dikeledi-K/AI_Solution.git
cd AI_Solution\clinicq
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
python -m core.backup new-key        # copy the key into .env as CLINICQ_BACKUP_KEY=...
pytest -q                            # all tests should pass
```

Edit `.env`:

```ini
CLINICQ_DEMO_MODE=0                  # NEVER 1 in a real clinic
CLINICQ_SETUP_CODE=<long random text> # needed once, to create the first administrator
CLINICQ_BACKUP_KEY=<from new-key>    # keep a second copy somewhere safe, away from the backups
LLM_PROVIDER=anthropic               # or gemini / groq; falls back safely when offline
ANTHROPIC_API_KEY=...
```

Edit `data/rooms.json` to match the clinic's rooms. Each room's `door` is the number painted on its
door – the only room name patients ever see.

## 4. Start ClinicQ and HTTPS automatically

1. Install **Caddy** (https://caddyserver.com) and copy `deploy/Caddyfile` next to it. Change
   `192.168.1.10` to the server's IP. In the router, add a local name `clinicq.lan` for that IP (or
   just use the IP address).
2. In **Task Scheduler**, create two tasks that run **at start-up**, whether anyone is signed in or not:
   - `deploy\start_clinicq.bat` – ClinicQ listens **only on 127.0.0.1**, so nothing reaches it except
     through Caddy.
   - `caddy run --config C:\path\to\Caddyfile` – serves `https://clinicq.lan` on port 443.
3. Windows Firewall: allow inbound **443** on the *private* network only. Do not open 8501.

## 5. Set up each tablet (once)

1. Connect to the staff Wi-Fi.
2. Install Caddy's local root certificate, so the tablet trusts `https://clinicq.lan`: copy
   `root.crt` from Caddy's data folder (`%AppData%\Caddy\pki\authorities\local\root.crt`) to the tablet,
   then *Settings → Security → Encryption & credentials → Install a certificate → CA certificate*.
3. Open Chrome at `https://clinicq.lan` → menu → **Add to Home screen**. It opens like an app.
4. Set a screen lock. ClinicQ also signs staff out after 30 minutes without activity.

**Reception tablet:** open **Patient Intake**; the registration clerk signs in with their own ID and PIN.
**Waiting-room TV:** open `https://clinicq.lan/Waiting_Room` full screen (F11 / kiosk mode). It needs no
sign-in and only shows queue numbers and door numbers.

## 6. First sign-in

Open **Staff Dashboard** → *First-time setup* → enter the setup code and create the administrator.
The administrator adds every staff member on the **Staff** page with a first PIN; each person must
choose their own PIN the first time they sign in.

## 7. Backups (every day)

1. Task Scheduler: run `deploy\backup_clinicq.bat` every day at 13:00 and 17:00. It writes an encrypted
   snapshot to `backups\` and keeps the last 30.
2. Every Friday, copy the `backups` folder to a USB stick that is kept **off site** (e.g. at the sub-district
   office). The files are encrypted, but treat the stick as confidential anyway.
3. Once a month, check that a backup can really be restored:
   ```powershell
   python -m core.backup verify backups\clinicq_YYYYMMDD_HHMMSS.db.enc
   ```
   `OK` means the backup decrypts, the database is complete and the audit log's hash chain is intact.
4. To restore after a disaster: `python -m core.backup restore <backup file> data\restored.db`, check it,
   then point `CLINICQ_DB_PATH` at the restored file and restart ClinicQ.

## 8. Every day

- The facility manager checks **Reports** (waiting times, nurse colour changes per language) and the
  **Audit Log** banner, which must say *Audit log intact*.
- Staff sign out when they leave a room; an admin can free a stuck room on the **Staff** page.
