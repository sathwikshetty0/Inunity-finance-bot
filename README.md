# Staff expense reimbursement Telegram bot

A no-frontend Telegram bot for collecting staff expense claims. FastAPI receives Telegram webhooks, `python-telegram-bot` runs the guided async conversation, Google Sheets stores submissions, and Google Drive stores bill files. Render is optional: the bot can be run locally with a public HTTPS tunnel.

## Features and how it works

- **Open access:** any Telegram user can use `/start` and `/new`; there is no Users-tab access gate.
- **Staff profile defaults:** if the user's Telegram ID is present in `Users`, the bot fills in the saved name, email, and default department. If not, it uses the user's Telegram display name, leaves email blank, and asks them to select a department. Name and email are never asked in chat.
- **Guided submission:** department, expense type, project, one to five bill photos/PDFs, confirmation or manual entry of bill date/vendor/amount, description, and reimbursement month. When the optional AI check is enabled, the bot tries to extract date, vendor, and amount from the first bill photo, and asks the user to confirm each extracted value. It asks for any value it could not read. PDFs are not scanned for details. Lists use buttons; projects are paginated. Month, expense type, and project can use Other where applicable.
- **Validation and warnings:** accepts `DD/MM/YYYY` or `today`, rejects future dates and amounts less than or equal to zero, strips `Rs`, `₹`, and commas from amounts. Claims older than 90 days and possible same-user/vendor/amount/date duplicates produce warnings but are not blocked.
- **Bills:** uploads go under `Reimbursements/YYYY-MM/<Name>/` in the configured Drive folder. Each upload is named `<bill-date>_<vendor>_<amount>_<number>.<extension>`. Files are limited to 20 MB each.
- **Submission record:** the bot appends one row to `Data` with a unique Submission ID, profile and expense information, Drive links, AI result/notes, and initial status `Pending`. Admins update Status and Admin Remarks directly in Sheets.
- **Optional staff/config tabs:** `Users` provides profile defaults and the recipients for weekly reminders; its `active` column affects reminders only, not bot access. Dropdowns come from `Config` and are cached for up to 10 minutes.
- **Dashboard:** `setup_sheet.py` creates a starter dashboard with formulas and charts. The current workbook's Dashboard can instead be customized with Gemini in Google Sheets. A Gemini-created visual dashboard can show total/pending/paid amounts, submission count, spend by department/project, status breakdown, and pending approvals. It should reference `Data` and leave the append-only data sheet unchanged.
- **Optional AI check and bill extraction:** off by default. When enabled, the first image bill is sent to the configured NVIDIA vision model to extract its date, vendor, and total amount; the user confirms or corrects each value before entering the description. The bot reports when automatic reading fails or fields are unclear, then asks for manual entry. The confirmed details are compared with the extraction at submission without making a second AI request, and the first photo download is reused for Drive upload. PDFs are not processed by AI. AI is advisory and never rejects a claim; the model is prompted not to guess, but extracted details must be confirmed by the user.
- **Admin notifications and reminders:** optionally DMs configured admins after submissions. The protected `/cron` endpoint sends weekly reminders to active `Users` rows that have no claim for the current month.
- **Health and retries:** `/health` is used for health checks. Telegram webhook requests are checked using `WEBHOOK_SECRET`; repeated update IDs are deduplicated in a bounded in-memory cache.

**Important storage constraint:** service accounts have no personal Drive storage quota. For bill uploads, `DRIVE_FOLDER_ID` must be a folder inside a Google Workspace **Shared Drive**, and the service account must be a Shared Drive member allowed to create files. Merely sharing a personal My Drive folder as Editor can still fail with `storageQuotaExceeded`. If Shared Drives are unavailable, a Workspace administrator must configure domain-wide delegation so uploads can be made as a licensed user.

**In-memory state:** drafts and the webhook deduplication cache are not database-backed. Restarting the process loses in-progress drafts and the retry cache. Completed submissions remain in Google Sheets. Do not run multiple app instances if relying on in-memory update deduplication.

## Google setup (one time)

1. Create/select a Google Cloud project and enable **Google Sheets API** and **Google Drive API**.
2. Create a service account and download a JSON key. Keep this key secret.
3. Create a Google spreadsheet and copy its ID from the URL:
   `https://docs.google.com/spreadsheets/d/<SHEET_ID>/edit`
4. Create or choose a folder inside a Google Workspace Shared Drive. Copy its ID from:
   `https://drive.google.com/drive/folders/<DRIVE_FOLDER_ID>`
5. Add the service account email (`client_email` in the JSON key) as an Editor of the spreadsheet. Add it as a Shared Drive member with permission to create files/folders.
6. Run `setup_sheet.py` once, as described below. It creates `Data`, `Users`, `Config`, and `Dashboard`, adds the Data headers and status validation, and writes starter dashboard formulas/charts.
7. In `Users`, optionally add rows with columns `telegram_id`, `name`, `email`, `default_department`, `active`. A matching user receives saved profile details/default department. Only active rows receive weekly reminders.
8. `Config` has `Category` and `Value` columns. Supported category values are `departments`, `expense_types`, and `projects`. The defaults include the provided department, expense type, and project lists. Config edits are read within 10 minutes.

### Dashboard and setup script warning

If you have already built a custom visual Dashboard with Gemini, keep it. **Do not rerun `setup_sheet.py` after customizing the workbook:** the script rewrites the `Config` values and the `Dashboard` starter content. It does not change existing Data rows, but it will replace dashboard formulas/content. The Gemini dashboard should use `Data` as its source and refer to the actual columns:

| Column | Field |
|---|---|
| A | Timestamp |
| B | Submission ID |
| C | Email |
| D | Name |
| E | Telegram ID |
| F | Month |
| G | Department |
| H | Expense Type |
| I | Description |
| J | Bill Date |
| K | Vendor |
| L | Amount |
| M | Project |
| N | Bill Links |
| O | AI Check Result |
| P | AI Notes |
| Q | Status |
| R | Admin Remarks |

## Telegram and secrets

1. In Telegram, message [@BotFather](https://t.me/BotFather), run `/newbot`, and save the token as `TELEGRAM_BOT_TOKEN`.
2. Create a random webhook secret of 1–256 letters, digits, underscores, or hyphens for `WEBHOOK_SECRET`.
3. Set `ADMIN_TELEGRAM_IDS` to comma-separated numeric Telegram IDs for admins who should receive notifications. It may be empty.
4. Create a separate long random value for `CRON_SECRET`.
5. Set these additional variables if needed:
   - `AI_CHECK_ENABLED=false` by default. To turn on AI checks, set it to `true` and provide `NVIDIA_API_KEY`.
   - `NVIDIA_MODEL` defaults to `meta/llama-3.2-11b-vision-instruct`.
   - `NOTIFY_ADMINS=true` by default; set `false` to disable admin messages.

Never paste credentials into chat or commit them. The example file contains placeholders only. Keep the actual credentials in an ignored local environment file such as `.env.local`, or set them in your deployment provider's secret settings.

## Run locally (no Render)

Telegram webhooks require a publicly reachable HTTPS address. Locally, use a tunnel such as ngrok. Keep both the bot process and the tunnel running while testing. The app does **not** automatically load `.env.local`; export its values into the PowerShell process before running the app.

### 1. Install dependencies

Use Python 3.11 as configured for deployment:

```powershell
cd C:\Projects\finance
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

If the virtual environment already exists, activate it and install any newly added requirements:

```powershell
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

If `py` is not recognized, install Python 3.11 and create the environment with that installation's full `python.exe` path, or use `python -m venv .venv` after confirming `python --version` is 3.11 or newer. The app requires Python 3.11 or newer.

### 2. Prepare `.env.local`

Create `.env.local` in the project root with the variables from `.env.example`, filled with real values. Use **IDs only**, not the complete Sheet or Drive URLs. The full service account JSON may be formatted across multiple lines. `.env.local` is ignored by Git.

### 3. Create the Sheet tabs once

In the same PowerShell window, load `.env.local` and run the setup script. The loader below supports a multi-line `GOOGLE_SERVICE_ACCOUNT_JSON` value:

```powershell
$lines = Get-Content ".env.local"
$values = @{}
for ($i = 0; $i -lt $lines.Count; $i++) {
    if ($lines[$i] -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$') {
        $name = $matches[1]
        $value = $matches[2]
        if ($name -eq "GOOGLE_SERVICE_ACCOUNT_JSON" -and $value.Trim() -eq "{") {
            $jsonLines = [System.Collections.Generic.List[string]]::new()
            $jsonLines.Add($value)
            $i++
            while ($i -lt $lines.Count -and $lines[$i].Trim() -ne "}") {
                $jsonLines.Add($lines[$i])
                $i++
            }
            if ($i -ge $lines.Count) { throw "GOOGLE_SERVICE_ACCOUNT_JSON is missing its closing brace" }
            $jsonLines.Add($lines[$i])
            $value = $jsonLines -join "`n"
        }
        $values[$name] = $value
    }
}
$null = $values["GOOGLE_SERVICE_ACCOUNT_JSON"] | ConvertFrom-Json -ErrorAction Stop
foreach ($entry in $values.GetEnumerator()) {
    [Environment]::SetEnvironmentVariable($entry.Key, $entry.Value, "Process")
}
python setup_sheet.py
```

Run `setup_sheet.py` only for initial workbook setup or if you intentionally want to restore its default Config and starter Dashboard. It is not needed every time the bot starts.

### 4. Start the HTTPS tunnel

Open a **second** PowerShell window and run:

```powershell
ngrok http 8000
```

Install ngrok first if needed. Copy the public HTTPS forwarding URL it displays, for example `https://abc123.ngrok-free.app`. Leave ngrok running. Free tunnel URLs may change after restarting ngrok.

### 5. Run the bot and register the webhook

Back in the PowerShell window where `.env.local` was loaded, set the URL to the **real** tunnel URL and start FastAPI:

```powershell
$env:RENDER_EXTERNAL_URL = "https://abc123.ngrok-free.app"
python -m uvicorn main:app --host 127.0.0.1 --port 8000
```

If Python is not on PATH or you prefer not to activate the environment, run the venv executable directly:

```powershell
.\.venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8000
```

The app registers Telegram's webhook as `<RENDER_EXTERNAL_URL>/telegram/webhook` when it starts. Keep this terminal running. Verify:

```powershell
Invoke-RestMethod "http://127.0.0.1:8000/health"
```

It should return `status: ok`. In Telegram, send `/start`, tap **Start a reimbursement** or send `/new`, answer the prompts, attach a bill, tap **Done**, review, and submit. If you restart the tunnel and its URL changes, update `RENDER_EXTERNAL_URL` and restart the bot so it registers the new URL.

### Local troubleshooting

- **No bot response:** confirm the bot and ngrok terminals are running, check `/health`, and verify Telegram's webhook points to the current tunnel URL plus `/telegram/webhook`.
- **Google API disabled / 403:** enable Sheets API and Drive API in the Cloud project that owns the service account key.
- **Sheets range or permission error:** run initial setup if tabs are missing; share the spreadsheet with the service account email as Editor.
- **Drive `storageQuotaExceeded`:** confirm `DRIVE_FOLDER_ID` is a folder inside a Shared Drive, not a personal My Drive folder. Confirm the service account is a Shared Drive member allowed to create files.
- **A restart lost the form:** drafts live in memory only; start a new one with `/new`.

## Commands, endpoints, and storage

| Command / endpoint | Purpose |
|---|---|
| `/start` | Welcome and instructions; open to any Telegram user. |
| `/new` | Begin a guided expense reimbursement. |
| `/cancel` | Discard the current in-memory draft. |
| `/mylast` | Show the user's latest five submissions and current Sheet status. |
| `GET /health` | App health check. |
| `POST /telegram/webhook` | Telegram webhook; requires `X-Telegram-Bot-Api-Secret-Token`. |
| `POST /cron` | Weekly reminder; requires `X-Cron-Secret`. |

The bot does not use a database or write uploaded bills to local disk. Bills are uploaded to Drive; submission rows and admin-maintained status/remarks live in Sheets. Status options are Pending, Approved, Paid, and Rejected.

## Optional Render deployment

Local operation does not require Render. To deploy, push the project to a Git provider, create a Render Web Service, and use the build/start commands in `render.yaml`. Add the required environment variables in Render's settings; Render supplies `RENDER_EXTERNAL_URL`, which the app uses to set the Telegram webhook. Configure `/health` as the health check. On Render's free tier the service may sleep when idle, so first webhook responses can be delayed while it wakes.

## Weekly reminders (optional)

Use a free external cron service to POST weekly to `https://<PUBLIC_HOST>/cron` with header `X-Cron-Secret: <CRON_SECRET>`. The endpoint DMs active rows in `Users` that have no Data submission for the current `YYYY-MM` month. Users absent from `Users` do not receive reminders.

## Test checklist

- [ ] `/health` returns HTTP 200 with `{"status":"ok"}`.
- [ ] `/start` works for users who are and are not in `Users`.
- [ ] A Users match fills name, email, and default department; an unlisted user gets Telegram display name and blank email.
- [ ] Month, department, expense type, project, Other text inputs, and project pagination work.
- [ ] Invalid/future bill dates, zero/negative amounts, unsupported files, files over 20 MB, and excess bills are rejected.
- [ ] One to five bills can be uploaded and submitted to a Shared Drive; a Pending row is appended to Data with the expected links and values.
- [ ] Duplicate and older-than-90-day claims warn but can continue; AI mismatch/unreadable is advisory, and AI outages save as Skipped.
- [ ] `/mylast` shows the current Sheet status after an admin changes it.
- [ ] Invalid webhook and cron secrets are rejected; repeated webhook update IDs are deduplicated while the process is running.
- [ ] The Gemini dashboard summarizes Data without editing or sorting the append-only Data sheet.
#   I n u n i t y - f i n a n c e - b o t  
 