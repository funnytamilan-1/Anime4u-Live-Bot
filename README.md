# Anime4u Telegram Storage Bot & MTProto Processing Engine

Automated large media processing, encoding, and Backblaze B2 storage engine with Telegram bot UI and MTProto worker integration.

---

## 🚀 Service Modes (`SERVICE_MODE`)

The bot operates under three distinct deployment profiles controlled by the `SERVICE_MODE` environment variable:

| Mode | Description | Telegram Polling | MTProto Worker Engine | Best For |
| :--- | :--- | :---: | :---: | :--- |
| **`all`** | **Unified Single-Instance** | ✅ Yes | ✅ Yes (Background Task) | **Single deployment on Railway, Render, or VPS** |
| **`render`** | Frontend Bot Only | ✅ Yes | ❌ No | Distributed setup (Render handles UI, separate worker node processes jobs) |
| **`worker`** | Headless Worker Node | ❌ No | ✅ Yes (Dedicated Lifecycle) | Dedicated background worker container on Railway / VPS |

---

## ⚙️ Railway Deployment Instructions

### 1. All-in-One Deployment (Recommended for Railway)
If you are deploying **only one service on Railway** that needs to both respond to Telegram commands (`/start`, `/folders`, sending files) and process uploads/downloads:

Set the following environment variable in your Railway dashboard:
```env
SERVICE_MODE=all
```

### 2. Environment Variables Required on Railway

| Variable | Description | Example / Default |
| :--- | :--- | :--- |
| `SERVICE_MODE` | Operational mode | `all` (or `worker` if running separate nodes) |
| `BOT_TOKEN` | Telegram Bot Token from `@BotFather` | `123456789:ABC...` |
| `ADMIN_IDS` | Comma-separated authorized Telegram user IDs | `5192451273, 8525952693` |
| `API_ID` | Telegram API ID from my.telegram.org | `12345678` |
| `API_HASH` | Telegram API Hash from my.telegram.org | `abcdef0123456789...` |
| `TELEGRAM_SESSION_STRING` | Optional string session to bypass rate limits | Generated via `--setup-session` |
| `B2_APPLICATION_KEY_ID` | Backblaze B2 Application Key ID | `005446...` |
| `B2_APPLICATION_KEY` | Backblaze B2 Application Key Secret | `K005...` |
| `B2_BUCKET_NAME` | Backblaze B2 Bucket Name | `anime4u-videos` |
| `B2_PUBLIC_BASE_URL` | Optional custom CDN / B2 domain | `https://f005.backblazeb2.com/file/anime4u-videos` |
| `DATABASE_PATH` | Path to persistent SQLite database | `/app/storage.db` (or persistent volume) |
| `SUPABASE_URL` | Optional Supabase URL for shared database | `https://xyz.supabase.co` |
| `SUPABASE_SERVICE_ROLE_KEY` | Optional Supabase service role key | `eyJhb...` |

---

## 🔍 Why Was The Bot Not Responding Before?

1. **`SERVICE_MODE=worker` Was Active**:
   - In `worker` mode, Telegram Bot API polling (`getUpdates`) is purposefully disabled to prevent duplicate polling conflicts (HTTP 409 Conflict) when running multiple distributed instances.
   - If your single Railway service is set to `SERVICE_MODE=worker`, the worker will only listen to the database queue for jobs, but will **not** listen for incoming `/start` messages or user commands in Telegram.
   - **Fix**: Change `SERVICE_MODE` to `all` in Railway variables.

2. **Admin Authorization**:
   - Ensure your personal Telegram user ID (e.g. from `@userinfobot`) is added to `ADMIN_IDS`. Unlisted IDs receive an authorization denial.
