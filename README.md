# Netflix Email Access — Railway Web App

A pure web-only replacement for the old Telegram bot. It includes:

- Railway-ready Flask app
- Dark Netflix-style admin UI
- Public email search with no visitor account
- Protected administrator login and settings panel
- Encrypted database-managed IMAP account
- Optional server-enforced private Sign-in Code access
- Configurable WhatsApp support contact
- Real-time admin activity feed using Socket.IO
- IMAP email search for Netflix Household, Reset, Login Code, Verification Code, Verify Email, and TV Login
- Search history, activity logs, renewal history
- Admin data export as downloadable ZIP/CSV
- SQLite database with Railway volume support
- Optional migration from old JSON files if they exist in the same Railway volume

## Administrator login

If you do not set environment variables, the first super admin is:

```txt
username: admin
password: admin123
```

Change this before real deployment.

## Railway deployment

1. Upload this folder to GitHub.
2. Create a Railway project from the GitHub repo.
3. Add a persistent volume.
4. Add environment variables from `.env.example`.
5. Deploy.

Railway will run this command from the `Procfile`:

```bash
gunicorn -w 1 --threads 8 app:app --bind 0.0.0.0:$PORT
```

## Required environment variables

```txt
SECRET_KEY=long-random-string
WEB_ADMIN_USERNAME=admin
WEB_ADMIN_PASSWORD=your-strong-password
IMAP_ENCRYPTION_KEY=generate-a-fernet-key
```

Generate the encryption key once and keep it stable across Railway deploys:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

`EMAIL_USER`, `EMAIL_PASS`, `IMAP_HOST`, and `IMAP_MAILBOX` remain supported for
first-start migration. Once an account exists in SQLite, the database account is
the source of truth. `IMAP_ENCRYPTION_KEY` is required to save or decrypt the
database App Password.

For Gmail, use an **App Password**, not your normal Gmail password.

## Public flow

1. Visitor opens `/` and enters an email address.
2. Visitor selects a category.
3. Sign-in Code requires the private code configured by an administrator when protection is enabled.
4. The server searches the enabled encrypted IMAP account and records anonymous search history.

## Admin features

- Dashboard with live stats
- Real-time feed
- Search statistics and admin-only logs
- IMAP account save, edit, delete, and connection test
- Sign-in Code protection settings
- WhatsApp support settings
- Download all app data as ZIP/CSV from Settings or Logs

## Legacy data

Old JSON files are not imported into the public accountless architecture. Existing SQLite tables are preserved safely and are unused by public search.

```txt
approved_users.json
user_email_assignments.json
```

Old Telegram IDs are stored as `legacy_telegram_id`, but Telegram is not used anywhere in this app.

## Run locally

```bash
python -m venv .venv
.venv\Scripts\activate   # Windows
# source .venv/bin/activate  # Linux/Mac
pip install -r requirements.txt
copy .env.example .env
python app.py
```

Open:

```txt
http://localhost:5000
```

## Important

This build intentionally removes all Telegram and visitor-account logic. No visitor credentials or email assignments are required.

## Patch: restored Email Bot 10 web features

This build restores the useful management tools from the Telegram bot version while keeping Telegram fully removed:

- Admin bulk removal from a user's profile.
- Admin bulk expiry editor: paste emails and select one expiry date.
- Admin per-line expiry editor: `email@example.com 2026-07-15`.
- Duplicate assignment confirmation: if an assigned email belongs to someone else, the app asks before overriding.
- Override assignment moves the email to the selected user.
- Email replacement tool: `old@example.com -> new@example.com`, preserving the old expiry date.
- Global email table bulk actions: remove selected, renew selected, set selected expiry.
- User-side “My Emails” page with a monthly expiry calendar.
- User-side email list sorted by expiry date.


## Latest patch: clickable user calendar + smoother responsive UI

- Users can open **My Emails** and click/tap any calendar date.
- A modal shows every email expiring on that date.
- Dates with no expiry show a clean empty state.
- Email list remains sorted by expiry date.
- Added smoother page/card/button transitions.
- Added mobile menu drawer and improved responsive behaviour for phones/tablets.

## Patch: Verification Code safety split

This build separates the old broad **Verification Code** search from the safer **Verification Code After Login** search.

- **Verification Code** is no longer auto-enabled for normal users.
- Admins can still use Verification Code directly.
- If an admin enables Verification Code for a normal user, the UI asks for a warning confirmation.
- **Verification Code After Login** is auto-enabled for every active normal user.
- The after-login extractor only returns a 6-digit code when the email body contains one of the known English, Indonesian, Thai, or Malay access-warning texts.

On first startup after this patch, old auto-granted normal-user Verification Code permissions are removed once for safety. Admin can enable it again manually per user.

## Railway start command note

This build intentionally does **not** use Eventlet. Railway is forced to start with:

```bash
gunicorn -w 1 --threads 8 app:app --bind 0.0.0.0:$PORT
```

If Railway Settings has an old custom Start Command, delete it or replace it with the command above. Then use **Redeploy without cache**.


## Admin login fix notes

Super admin can login using either `WEB_ADMIN_USERNAME`, `WEB_ADMIN_EMAIL`, or `EMAIL_USER`. The super-admin password is synced from `WEB_ADMIN_PASSWORD` on every app startup, so changing the Railway variable updates the admin login after redeploy. Avoid wrapping Railway variable values in quotes.
