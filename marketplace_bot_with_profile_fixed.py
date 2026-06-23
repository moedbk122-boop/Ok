import logging
import os
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta
from typing import Dict, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Update
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ------------------------------------------------------------
# Configuration
# ------------------------------------------------------------
TOKEN = "8989225920:AAFHHwBb5JSJ8cVmw2e8yCov0tIb6D_sC94"

ADMIN_IDS = [7935141097]
MAIN_CHAT_ID = -1003965557035

DENY_COOLDOWN_SECONDS = 120
POST_COOLDOWN_SECONDS = 1200

DB_PATH = "marketplace.db"
USER_LOG_PATH = "users.log"
MONITOR_LOG_PATH = "server_monitor.log"

# Normal post states
USER_AWAITING_DESCRIPTION = "awaiting_description"
USER_PENDING_DESCRIPTION = "pending_description"
USER_AWAITING_IMAGE = "awaiting_image"
USER_PENDING_IMAGE = "pending_image"
USER_CONFIRMING_POST = "confirming_post"

# Schedule states
USER_AWAITING_SCHEDULE_TIME = "awaiting_schedule_time"
USER_SCHEDULE_POST_COUNT = "schedule_post_count"
USER_SCHEDULE_CURRENT_POST = "schedule_current_post"
USER_SCHEDULE_POSTS = "schedule_posts"
USER_SCHEDULE_TIME = "schedule_time"
USER_SCHEDULE_AWAITING_DESCRIPTION = "schedule_awaiting_description"
USER_SCHEDULE_AWAITING_IMAGE = "schedule_awaiting_image"

MONITOR_INTERVAL = 60
SCHEDULE_CHECK_INTERVAL = 30

# Telegram limits. Captions are limited to 1024 characters and text to 4096.
MAX_DESCRIPTION_LENGTH = 900

# ------------------------------------------------------------
# Logging
# ------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)
logger.info("Logging setup complete.")

LOG_LOCK = threading.Lock()


def utcnow() -> datetime:
    return datetime.utcnow()


def write_log(path: str, content: str) -> None:
    with LOG_LOCK:
        with open(path, "a", encoding="utf-8") as log_file:
            log_file.write(content)


# ------------------------------------------------------------
# Activity Logging Functions
# ------------------------------------------------------------
def log_user_activity(user_id: int, username: str, action: str, details: str = ""):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_entry = (
        f"[{timestamp}] User: @{username if username else 'no_username'} "
        f"(ID: {user_id}) - {action}"
    )
    if details:
        log_entry += f" - {details}"
    write_log(USER_LOG_PATH, log_entry + "\n")
    logger.info("User Activity: %s", log_entry)


def log_post_published(user_id: int, username: str, post_content: str, has_image: bool):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    image_status = "with image" if has_image else "without image"
    truncated_content = post_content[:200] + "..." if len(post_content) > 200 else post_content
    log_entry = (
        f"[{timestamp}] POST PUBLISHED - User: "
        f"@{username if username else 'no_username'} (ID: {user_id}) - {image_status}\n"
        f"Content: {truncated_content}\n"
        f"{'-' * 80}\n"
    )
    write_log(USER_LOG_PATH, log_entry)
    logger.info("Post Published: @%s (ID: %s) - %s", username, user_id, image_status)


def log_daily_reset():
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_entry = (
        f"[{timestamp}] DAILY RESET - All user post counts have been reset for the new day\n"
        f"{'-' * 80}\n"
    )
    write_log(USER_LOG_PATH, log_entry)
    logger.info("Daily post counts reset")


def log_license_update(user_id: int, username: str, action: str, details: str = ""):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_entry = (
        f"[{timestamp}] LICENSE - User: @{username if username else 'no_username'} "
        f"(ID: {user_id}) - {action}"
    )
    if details:
        log_entry += f" - {details}"
    write_log(USER_LOG_PATH, log_entry + "\n")
    logger.info("License Update: %s for user %s", action, user_id)


def log_scheduled_post(user_id: int, username: str, scheduled_time: str, post_count: int):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_entry = (
        f"[{timestamp}] POSTS SCHEDULED - User: "
        f"@{username if username else 'no_username'} (ID: {user_id}) - "
        f"{post_count} posts scheduled for {scheduled_time}\n"
    )
    write_log(USER_LOG_PATH, log_entry)
    logger.info("Scheduled Posts: %s posts for %s by user %s", post_count, scheduled_time, user_id)


def log_scheduled_post_published(
    user_id: int,
    username: str,
    scheduled_time: str,
    post_index: int,
    total_posts: int,
):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_entry = (
        f"[{timestamp}] SCHEDULED POST PUBLISHED - User: "
        f"@{username if username else 'no_username'} (ID: {user_id}) - "
        f"Post {post_index + 1}/{total_posts} from schedule at {scheduled_time}\n"
    )
    write_log(USER_LOG_PATH, log_entry)
    logger.info(
        "Scheduled Post Published: %s/%s for user %s",
        post_index + 1,
        total_posts,
        user_id,
    )


def get_recent_logs(lines: int = 50) -> str:
    try:
        with LOG_LOCK:
            with open(USER_LOG_PATH, "r", encoding="utf-8") as log_file:
                all_lines = log_file.readlines()
        return "".join(all_lines if len(all_lines) <= lines else all_lines[-lines:])
    except FileNotFoundError:
        return "Log file not found yet. It will be created when the first activity occurs."
    except Exception as exc:
        return f"Error reading log file: {exc}"


# ------------------------------------------------------------
# Database Setup
# ------------------------------------------------------------
def init_db():
    with sqlite3.connect(DB_PATH, timeout=30) as con:
        cur = con.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS licenses (
                user_id INTEGER PRIMARY KEY,
                expires_at TEXT NOT NULL,
                posts_per_day INTEGER NOT NULL,
                posts_today INTEGER DEFAULT 0,
                posts_today_date TEXT
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS blacklist (
                user_id INTEGER PRIMARY KEY
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS cooldowns (
                user_id INTEGER PRIMARY KEY,
                deny_until TEXT
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT,
                description TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS scheduled_posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT,
                description TEXT NOT NULL,
                image_file_id TEXT,
                scheduled_time TEXT NOT NULL,
                post_index INTEGER NOT NULL,
                total_posts INTEGER NOT NULL,
                status TEXT DEFAULT 'pending',
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES licenses(user_id)
            )
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_scheduled_posts
            ON scheduled_posts(scheduled_time, status)
            """
        )


def db():
    con = sqlite3.connect(DB_PATH, timeout=30)
    return con


# ------------------------------------------------------------
# Scheduled Posts Database Functions
# ------------------------------------------------------------
def add_scheduled_post(
    user_id: int,
    username: str,
    description: str,
    image_file_id: Optional[str],
    scheduled_time: str,
    post_index: int,
    total_posts: int,
):
    con = db()
    try:
        cur = con.cursor()
        cur.execute(
            """
            INSERT INTO scheduled_posts
            (user_id, username, description, image_file_id, scheduled_time,
             post_index, total_posts, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                username,
                description,
                image_file_id,
                scheduled_time,
                post_index,
                total_posts,
                utcnow().isoformat(),
            ),
        )
        con.commit()
        return cur.lastrowid
    finally:
        con.close()


def get_pending_scheduled_posts(limit: int = 10):
    con = db()
    try:
        cur = con.cursor()
        cur.execute(
            """
            SELECT id, user_id, username, description, image_file_id,
                   scheduled_time, post_index, total_posts
            FROM scheduled_posts
            WHERE status = 'pending' AND scheduled_time <= ?
            ORDER BY scheduled_time, post_index
            LIMIT ?
            """,
            (utcnow().isoformat(), limit),
        )
        return cur.fetchall()
    finally:
        con.close()


def mark_scheduled_post_as_published(post_id: int):
    con = db()
    try:
        con.execute(
            "UPDATE scheduled_posts SET status = 'published' WHERE id = ?",
            (post_id,),
        )
        con.commit()
    finally:
        con.close()


def get_user_scheduled_posts(user_id: int):
    con = db()
    try:
        cur = con.cursor()
        cur.execute(
            """
            SELECT id, description, scheduled_time, post_index, total_posts, status
            FROM scheduled_posts
            WHERE user_id = ?
            ORDER BY scheduled_time, post_index
            """,
            (user_id,),
        )
        return cur.fetchall()
    finally:
        con.close()


def delete_scheduled_post(post_id: int, user_id: int):
    con = db()
    try:
        cur = con.cursor()
        cur.execute(
            "DELETE FROM scheduled_posts WHERE id = ? AND user_id = ?",
            (post_id, user_id),
        )
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()


def clear_user_scheduled_posts(user_id: int):
    con = db()
    try:
        cur = con.cursor()
        cur.execute("DELETE FROM scheduled_posts WHERE user_id = ?", (user_id,))
        con.commit()
        return cur.rowcount
    finally:
        con.close()


# ------------------------------------------------------------
# Server Monitoring Functions
# ------------------------------------------------------------
def log_server_status():
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        con = db()
        cur = con.cursor()

        cur.execute("SELECT COUNT(*) FROM licenses")
        total_licenses = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM licenses WHERE datetime(expires_at) > datetime('now')")
        active_licenses = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM licenses WHERE datetime(expires_at) <= datetime('now')")
        expired_licenses = cur.fetchone()[0]

        today = date.today().isoformat()
        cur.execute("SELECT SUM(posts_today) FROM licenses WHERE posts_today_date = ?", (today,))
        today_posts = cur.fetchone()[0] or 0

        cur.execute("SELECT COUNT(*) FROM scheduled_posts WHERE status = 'pending'")
        pending_scheduled = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM scheduled_posts WHERE status = 'published'")
        published_scheduled = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM blacklist")
        blacklisted_users = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM cooldowns WHERE datetime(deny_until) > datetime('now')")
        active_cooldowns = cur.fetchone()[0]

        cur.execute(
            """
            SELECT user_id, posts_per_day, posts_today, expires_at, posts_today_date
            FROM licenses
            WHERE datetime(expires_at) > datetime('now')
            ORDER BY user_id
            """
        )
        active_license_details = cur.fetchall()

        three_days_later = (utcnow() + timedelta(days=3)).isoformat()
        cur.execute(
            """
            SELECT COUNT(*) FROM licenses
            WHERE datetime(expires_at) > datetime('now')
              AND datetime(expires_at) <= datetime(?)
            """,
            (three_days_later,),
        )
        expiring_soon = cur.fetchone()[0]

        cur.execute(
            """
            SELECT user_id, posts_per_day, posts_today
            FROM licenses
            WHERE posts_today >= posts_per_day * 0.5
              AND posts_today_date = ?
              AND datetime(expires_at) > datetime('now')
            """,
            (today,),
        )
        high_usage_users = cur.fetchall()
        con.close()

        monitor_entry = f"\n{'=' * 80}\nSERVER STATUS - {timestamp}\n{'=' * 80}\n"
        monitor_entry += "SUMMARY STATISTICS:\n"
        monitor_entry += f"  Total Licenses: {total_licenses}\n"
        monitor_entry += f"  Active Licenses: {active_licenses}\n"
        monitor_entry += f"  Expired Licenses: {expired_licenses}\n"
        monitor_entry += f"  Licenses Expiring Soon (<3 days): {expiring_soon}\n"
        monitor_entry += f"  Today's Total Posts: {today_posts}\n"
        monitor_entry += f"  Scheduled Posts (Pending): {pending_scheduled}\n"
        monitor_entry += f"  Scheduled Posts (Published): {published_scheduled}\n"
        monitor_entry += f"  Blacklisted Users: {blacklisted_users}\n"
        monitor_entry += f"  Active Cooldowns: {active_cooldowns}\n"

        monitor_entry += f"\nACTIVE LICENSES ({len(active_license_details)} users):\n"
        now_utc = utcnow()
        for user_id, posts_per_day, posts_today, expires_at, _ in active_license_details:
            expires_date = datetime.fromisoformat(expires_at)
            days_remaining = max(0, (expires_date - now_utc).days)
            posts_today = posts_today or 0
            posts_remaining = max(0, posts_per_day - posts_today)
            usage_percentage = (posts_today / posts_per_day * 100) if posts_per_day > 0 else 0
            monitor_entry += (
                f"  User ID: {user_id} | Daily: {posts_today}/{posts_per_day} "
                f"({usage_percentage:.1f}%) | Remaining: {posts_remaining} | "
                f"Expires in: {days_remaining} days\n"
            )

        if high_usage_users:
            monitor_entry += "\nHIGH USAGE USERS (>50% daily limit):\n"
            for user_id, posts_per_day, posts_today in high_usage_users:
                usage_percentage = (posts_today / posts_per_day * 100) if posts_per_day > 0 else 0
                monitor_entry += (
                    f"  User ID: {user_id} | Usage: {posts_today}/{posts_per_day} "
                    f"({usage_percentage:.1f}%)\n"
                )

        monitor_entry += "\nSYSTEM HEALTH:\n"
        monitor_entry += f"  Database: {DB_PATH} ({os.path.getsize(DB_PATH) / 1024:.2f} KB)\n"
        monitor_entry += f"  User Log: {USER_LOG_PATH} ({os.path.getsize(USER_LOG_PATH) / 1024:.2f} KB)\n"
        monitor_size = os.path.getsize(MONITOR_LOG_PATH) if os.path.exists(MONITOR_LOG_PATH) else 0
        monitor_entry += f"  Monitor Log: {MONITOR_LOG_PATH} ({monitor_size / 1024:.2f} KB)\n"
        monitor_entry += f"  Current Time: {timestamp}\n{'=' * 80}\n\n"

        write_log(MONITOR_LOG_PATH, monitor_entry)
        logger.info("Server status logged at %s", timestamp)
    except Exception as exc:
        logger.exception("Error logging server status")
        write_log(MONITOR_LOG_PATH, f"\n[ERROR {timestamp}] Failed to log server status: {exc}\n")


def get_detailed_database_status() -> Dict:
    status = {
        "timestamp": datetime.now().isoformat(),
        "licenses": {"total": 0, "active": 0, "expired": 0},
        "posts": {
            "today": 0,
            "yesterday": 0,
            "scheduled_pending": 0,
            "scheduled_published": 0,
        },
        "users": {"blacklisted": 0, "cooldown": 0},
        "active_users": [],
        "system": {},
    }

    try:
        con = db()
        cur = con.cursor()
        cur.execute("SELECT COUNT(*) FROM licenses")
        status["licenses"]["total"] = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM licenses WHERE datetime(expires_at) > datetime('now')")
        status["licenses"]["active"] = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM licenses WHERE datetime(expires_at) <= datetime('now')")
        status["licenses"]["expired"] = cur.fetchone()[0]

        today = date.today().isoformat()
        cur.execute("SELECT SUM(posts_today) FROM licenses WHERE posts_today_date = ?", (today,))
        status["posts"]["today"] = cur.fetchone()[0] or 0

        yesterday = (date.today() - timedelta(days=1)).isoformat()
        cur.execute("SELECT SUM(posts_today) FROM licenses WHERE posts_today_date = ?", (yesterday,))
        status["posts"]["yesterday"] = cur.fetchone()[0] or 0

        cur.execute("SELECT COUNT(*) FROM scheduled_posts WHERE status = 'pending'")
        status["posts"]["scheduled_pending"] = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM scheduled_posts WHERE status = 'published'")
        status["posts"]["scheduled_published"] = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM blacklist")
        status["users"]["blacklisted"] = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM cooldowns WHERE datetime(deny_until) > datetime('now')")
        status["users"]["cooldown"] = cur.fetchone()[0]

        cur.execute(
            """
            SELECT user_id, posts_per_day, posts_today, expires_at, posts_today_date
            FROM licenses
            WHERE datetime(expires_at) > datetime('now')
            ORDER BY posts_today DESC
            """
        )

        active_users = []
        now_utc = utcnow()
        for user_id, posts_per_day, posts_today, expires_at, _ in cur.fetchall():
            posts_today = posts_today or 0
            expires_date = datetime.fromisoformat(expires_at)
            active_users.append(
                {
                    "user_id": user_id,
                    "posts_per_day": posts_per_day,
                    "posts_today": posts_today,
                    "posts_remaining": max(0, posts_per_day - posts_today),
                    "usage_percentage": (
                        posts_today / posts_per_day * 100 if posts_per_day > 0 else 0
                    ),
                    "days_remaining": max(0, (expires_date - now_utc).days),
                    "expires_at": expires_at,
                }
            )
        status["active_users"] = active_users

        status["system"]["database_size"] = os.path.getsize(DB_PATH)
        status["system"]["log_size"] = os.path.getsize(USER_LOG_PATH)
        status["system"]["monitor_size"] = (
            os.path.getsize(MONITOR_LOG_PATH) if os.path.exists(MONITOR_LOG_PATH) else 0
        )
        con.close()
    except Exception as exc:
        logger.exception("Error getting database status")
        status["error"] = str(exc)

    return status


def start_monitoring():
    def monitor_loop():
        while True:
            try:
                log_server_status()
            except Exception:
                logger.exception("Error in monitoring loop")
            time.sleep(MONITOR_INTERVAL)

    monitor_thread = threading.Thread(target=monitor_loop, daemon=True)
    monitor_thread.start()
    logger.info("Server monitoring started (interval: %ss)", MONITOR_INTERVAL)


def format_detailed_status(status: Dict) -> str:
    if "error" in status:
        return f"Error getting status: {status['error']}"

    timestamp = datetime.fromisoformat(status["timestamp"]).strftime("%Y-%m-%d %H:%M:%S")
    message = f"SERVER STATUS - {timestamp}\n\nSUMMARY\n"
    message += f"Total Licenses: {status['licenses']['total']}\n"
    message += f"Active Licenses: {status['licenses']['active']}\n"
    message += f"Expired Licenses: {status['licenses']['expired']}\n"
    message += f"Today's Posts: {status['posts']['today']}\n"
    message += f"Yesterday's Posts: {status['posts']['yesterday']}\n"
    message += f"Scheduled Posts (Pending): {status['posts']['scheduled_pending']}\n"
    message += f"Scheduled Posts (Published): {status['posts']['scheduled_published']}\n"
    message += f"Blacklisted Users: {status['users']['blacklisted']}\n"
    message += f"Users on Cooldown: {status['users']['cooldown']}\n\n"

    if status["active_users"]:
        message += "ACTIVE USERS (Top 10 by Usage)\n"
        sorted_users = sorted(
            status["active_users"],
            key=lambda item: item["usage_percentage"],
            reverse=True,
        )[:10]
        for index, user in enumerate(sorted_users, 1):
            usage_bar = "#" * min(10, int(user["usage_percentage"] / 10))
            usage_bar += "-" * (10 - len(usage_bar))
            message += (
                f"{index}. {user['user_id']} - {user['posts_today']}/{user['posts_per_day']} "
                f"({user['usage_percentage']:.1f}%)\n"
                f"   {usage_bar} | {user['days_remaining']}d remaining\n"
            )

    message += "\nSYSTEM\n"
    message += f"Database: {status['system']['database_size'] / 1024:.1f} KB\n"
    message += f"User Log: {status['system']['log_size'] / 1024:.1f} KB\n"
    message += f"Monitor Log: {status['system']['monitor_size'] / 1024:.1f} KB\n"
    message += f"\nUpdated every {MONITOR_INTERVAL} seconds"
    return message

# ------------------------------------------------------------
# Helper Functions
# ------------------------------------------------------------
def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


def is_blacklisted(uid: int) -> bool:
    con = db()
    try:
        row = con.execute("SELECT 1 FROM blacklist WHERE user_id = ?", (uid,)).fetchone()
        return bool(row)
    finally:
        con.close()


def add_blacklist(uid: int):
    con = db()
    try:
        con.execute("INSERT OR IGNORE INTO blacklist (user_id) VALUES (?)", (uid,))
        con.commit()
    finally:
        con.close()
    log_user_activity(uid, "unknown", "BLACKLISTED", "User added to blacklist")


def remove_blacklist(uid: int):
    con = db()
    try:
        con.execute("DELETE FROM blacklist WHERE user_id = ?", (uid,))
        con.commit()
    finally:
        con.close()
    log_user_activity(uid, "unknown", "REMOVED FROM BLACKLIST", "User removed from blacklist")


def create_license(uid: int, days: int, posts_per_day: int):
    expires_at = (utcnow() + timedelta(days=days)).isoformat()
    today = date.today().isoformat()
    con = db()
    try:
        con.execute(
            """
            INSERT INTO licenses
                (user_id, expires_at, posts_per_day, posts_today, posts_today_date)
            VALUES (?, ?, ?, 0, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                expires_at = excluded.expires_at,
                posts_per_day = excluded.posts_per_day,
                posts_today_date = excluded.posts_today_date
            """,
            (uid, expires_at, posts_per_day, today),
        )
        con.commit()
    finally:
        con.close()
    log_license_update(
        uid,
        "unknown",
        "LICENSE CREATED/UPDATED",
        f"{days} days, {posts_per_day} posts/day",
    )


def get_license(uid: int) -> Optional[dict]:
    con = db()
    try:
        row = con.execute(
            """
            SELECT user_id, expires_at, posts_per_day, posts_today, posts_today_date
            FROM licenses WHERE user_id = ?
            """,
            (uid,),
        ).fetchone()
    finally:
        con.close()

    if not row:
        return None
    user_id, expires_at, posts_per_day, posts_today, posts_today_date = row
    return {
        "user_id": user_id,
        "expires_at": expires_at,
        "posts_per_day": posts_per_day,
        "posts_today": posts_today or 0,
        "posts_today_date": posts_today_date,
    }


def reset_daily_counts_if_needed(uid: int):
    con = db()
    try:
        today = date.today().isoformat()
        row = con.execute(
            "SELECT posts_today_date FROM licenses WHERE user_id = ?", (uid,)
        ).fetchone()
        if row and row[0] != today:
            con.execute(
                """
                UPDATE licenses SET posts_today = 0, posts_today_date = ?
                WHERE user_id = ?
                """,
                (today, uid),
            )
            con.commit()
            log_user_activity(
                uid,
                "unknown",
                "DAILY POST COUNT RESET",
                f"Reset to 0 for date {today}",
            )
    finally:
        con.close()


def reset_all_daily_counts():
    con = db()
    try:
        today = date.today().isoformat()
        con.execute(
            """
            UPDATE licenses SET posts_today = 0, posts_today_date = ?
            WHERE posts_today_date != ? OR posts_today_date IS NULL
            """,
            (today, today),
        )
        con.commit()
    finally:
        con.close()
    log_daily_reset()
    logger.info("Reset daily post counts for all users for date: %s", today)


def increment_post_count(uid: int):
    con = db()
    try:
        today = date.today().isoformat()
        row = con.execute(
            "SELECT posts_today, posts_today_date FROM licenses WHERE user_id = ?",
            (uid,),
        ).fetchone()
        if not row:
            return
        posts_today, posts_today_date = row
        posts_today = posts_today or 0
        if posts_today_date != today:
            posts_today = 0
        con.execute(
            """
            UPDATE licenses SET posts_today = ?, posts_today_date = ?
            WHERE user_id = ?
            """,
            (posts_today + 1, today, uid),
        )
        con.commit()
    finally:
        con.close()


def list_licenses():
    con = db()
    try:
        return con.execute(
            """
            SELECT user_id, expires_at, posts_per_day, posts_today, posts_today_date
            FROM licenses
            """
        ).fetchall()
    finally:
        con.close()


def revoke_license(uid: int):
    con = db()
    try:
        con.execute("DELETE FROM licenses WHERE user_id = ?", (uid,))
        con.commit()
    finally:
        con.close()
    log_license_update(uid, "unknown", "LICENSE REVOKED", "License removed from user")


def set_cooldown(uid: int, until: datetime):
    con = db()
    try:
        con.execute(
            """
            INSERT INTO cooldowns (user_id, deny_until) VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET deny_until = excluded.deny_until
            """,
            (uid, until.isoformat()),
        )
        con.commit()
    finally:
        con.close()
    duration = max(0, int((until - utcnow()).total_seconds()))
    log_user_activity(uid, "unknown", "COOLDOWN SET", f"{duration} seconds until {until.isoformat()}")


def get_cooldown(uid: int) -> Optional[datetime]:
    con = db()
    try:
        row = con.execute(
            "SELECT deny_until FROM cooldowns WHERE user_id = ?", (uid,)
        ).fetchone()
    finally:
        con.close()
    return datetime.fromisoformat(row[0]) if row else None


def add_pending_post(uid: int, username: str, desc: str) -> int:
    con = db()
    try:
        cur = con.cursor()
        cur.execute(
            """
            INSERT INTO pending_posts (user_id, username, description, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (uid, username, desc, utcnow().isoformat()),
        )
        con.commit()
        return cur.lastrowid
    finally:
        con.close()


def get_pending_post(pid: int):
    con = db()
    try:
        return con.execute(
            """
            SELECT id, user_id, username, description, created_at
            FROM pending_posts WHERE id = ?
            """,
            (pid,),
        ).fetchone()
    finally:
        con.close()


def remove_pending_post(pid: int):
    con = db()
    try:
        con.execute("DELETE FROM pending_posts WHERE id = ?", (pid,))
        con.commit()
    finally:
        con.close()


def clear_post_flow(context: ContextTypes.DEFAULT_TYPE):
    for key in (
        USER_PENDING_DESCRIPTION,
        USER_AWAITING_DESCRIPTION,
        USER_PENDING_IMAGE,
        USER_AWAITING_IMAGE,
        USER_CONFIRMING_POST,
    ):
        context.user_data.pop(key, None)


def clear_schedule_flow(context: ContextTypes.DEFAULT_TYPE):
    for key in (
        USER_SCHEDULE_POST_COUNT,
        USER_SCHEDULE_CURRENT_POST,
        USER_SCHEDULE_POSTS,
        USER_SCHEDULE_TIME,
        USER_AWAITING_SCHEDULE_TIME,
        USER_SCHEDULE_AWAITING_DESCRIPTION,
        USER_SCHEDULE_AWAITING_IMAGE,
    ):
        context.user_data.pop(key, None)


def validate_description(description: str) -> Optional[str]:
    if not description:
        return "Description cannot be empty."
    if len(description) > MAX_DESCRIPTION_LENGTH:
        return (
            f"Description is too long. Please keep it under {MAX_DESCRIPTION_LENGTH} "
            "characters so it also works as an image caption."
        )
    return None


async def send_long_text(message, text: str, chunk_size: int = 3900):
    for start_index in range(0, len(text), chunk_size):
        await message.reply_text(text[start_index : start_index + chunk_size])


async def edit_result_message(query, text: str, has_image: bool):
    try:
        if has_image:
            await query.edit_message_caption(caption=text, reply_markup=None)
        else:
            await query.edit_message_text(text=text, reply_markup=None)
    except Exception:
        logger.exception("Could not edit confirmation result message")
        try:
            await contextless_reply(query, text)
        except Exception:
            logger.exception("Could not send fallback result message")


async def contextless_reply(query, text: str):
    if query.message:
        await query.message.reply_text(text)


# ------------------------------------------------------------
# Scheduled Post Checker
# ------------------------------------------------------------
async def check_scheduled_posts(context: ContextTypes.DEFAULT_TYPE):
    try:
        pending_posts = get_pending_scheduled_posts(limit=10)
        for post in pending_posts:
            (
                post_id,
                user_id,
                username,
                description,
                image_file_id,
                scheduled_time,
                post_index,
                total_posts,
            ) = post

            license_info = get_license(user_id)
            if not license_info:
                mark_scheduled_post_as_published(post_id)
                continue

            expires = datetime.fromisoformat(license_info["expires_at"])
            if utcnow() > expires:
                mark_scheduled_post_as_published(post_id)
                continue

            reset_daily_counts_if_needed(user_id)
            license_info = get_license(user_id)
            if not license_info or license_info["posts_today"] >= license_info["posts_per_day"]:
                continue

            footer = f"\n\nPosted by @{username}" if username else f"\n\nPosted by User {user_id}"
            full_description = description + footer

            try:
                if image_file_id:
                    await context.bot.send_photo(
                        chat_id=MAIN_CHAT_ID,
                        photo=image_file_id,
                        caption=full_description,
                    )
                else:
                    await context.bot.send_message(
                        chat_id=MAIN_CHAT_ID,
                        text=full_description,
                    )

                increment_post_count(user_id)
                mark_scheduled_post_as_published(post_id)
                log_scheduled_post_published(
                    user_id,
                    username,
                    scheduled_time,
                    post_index,
                    total_posts,
                )

                if post_index + 1 == total_posts:
                    try:
                        await context.bot.send_message(
                            chat_id=user_id,
                            text=(
                                f"All {total_posts} scheduled posts have been "
                                "published successfully!"
                            ),
                        )
                    except Exception:
                        logger.info("Could not notify user %s about completed schedule", user_id)

                logger.info(
                    "Published scheduled post %s/%s for user %s",
                    post_index + 1,
                    total_posts,
                    user_id,
                )
            except Exception:
                logger.exception("Failed to publish scheduled post %s", post_id)
    except Exception:
        logger.exception("Error in schedule checker")


# ------------------------------------------------------------
# Basic and Admin Commands
# ------------------------------------------------------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    message = (
        "Marketplace Bot\n\n"
        "/post - Create and publish a post\n"
        "/schedule - Schedule up to 3 posts\n"
        "/myschedule - View your scheduled posts\n"
        "/deleteschedule <post_id> - Delete a scheduled post\n"
        "/license - View your license\n"
        "/profile - View your Telegram ID and license status"
    )
    if is_admin(uid):
        message += (
            "\n\nAdmin commands:\n"
            "/newlicense <user_id> <days> <posts_per_day>\n"
            "/licenses\n"
            "/remaining <user_id>\n"
            "/revoke <@username>\n"
            "/revokelicense <user_id>\n"
            "/blacklist <user_id>\n"
            "/unblacklist <user_id>\n"
            "/viewlogs [lines]\n"
            "/monitor\n"
            "/serverlogs [entries]"
        )
    await update.message.reply_text(message)


async def newlicense_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text("Not authorized.")
        return
    if len(context.args) != 3:
        await update.message.reply_text(
            "Usage: /newlicense <user_id> <days> <posts_per_day>"
        )
        return
    try:
        target = int(context.args[0])
        days = int(context.args[1])
        posts_per_day = int(context.args[2])
        if days <= 0 or posts_per_day <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("User ID, days, and posts per day must be positive numbers.")
        return

    create_license(target, days, posts_per_day)
    await update.message.reply_text(
        f"License created for user {target}: {days} days, {posts_per_day} posts/day."
    )


async def blacklist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text("Not authorized.")
        return
    if len(context.args) < 1:
        await update.message.reply_text("/blacklist <user_id>")
        return
    try:
        target = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Invalid user ID.")
        return
    add_blacklist(target)
    await update.message.reply_text(f"User {target} has been blacklisted.")


async def unblacklist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text("Not authorized.")
        return
    if len(context.args) < 1:
        await update.message.reply_text("/unblacklist <user_id>")
        return
    try:
        target = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Invalid user ID.")
        return
    remove_blacklist(target)
    await update.message.reply_text(f"User {target} has been removed from blacklist.")


async def licenses_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text("Not authorized.")
        return

    records = list_licenses()
    if not records:
        await update.message.reply_text("No licenses.")
        return

    now = utcnow()
    lines = []
    for target_uid, expires_at, ppd, used, _ in records:
        expires = datetime.fromisoformat(expires_at)
        status = "ACTIVE" if now <= expires else "EXPIRED"
        try:
            user = await context.bot.get_chat(target_uid)
            username = f"@{user.username}" if user.username else f"User {target_uid}"
        except Exception:
            username = f"User {target_uid}"
        lines.append(
            f"{username} | Expires: {expires_at} | Daily: {used or 0}/{ppd} | Status: {status}"
        )

    await send_long_text(update.message, "\n".join(lines))


async def remaining(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text("Not authorized.")
        return
    if len(context.args) < 1:
        await update.message.reply_text("/remaining user_id")
        return
    try:
        target = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Invalid user ID.")
        return

    lic = get_license(target)
    if not lic:
        await update.message.reply_text("No license for that user.")
        return

    reset_daily_counts_if_needed(target)
    lic = get_license(target)
    remaining_posts = max(0, lic["posts_per_day"] - lic["posts_today"])
    expires_date = datetime.fromisoformat(lic["expires_at"])
    days_remaining = max(0, (expires_date - utcnow()).days)
    await update.message.reply_text(
        f"License Information for User {target}\n\n"
        f"Days remaining: {days_remaining}\n"
        f"Posts remaining today: {remaining_posts}\n"
        f"Daily limit: {lic['posts_per_day']} posts\n"
        f"License expires: {expires_date.strftime('%Y-%m-%d')}"
    )


async def revoke_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text("Not authorized.")
        return
    if len(context.args) < 1:
        await update.message.reply_text("/revoke @username")
        return

    username_input = context.args[0].strip().lstrip("@")
    try:
        target_user = None
        for user_id, *_ in list_licenses():
            try:
                user = await context.bot.get_chat(user_id)
                if user.username and user.username.lower() == username_input.lower():
                    target_user = user
                    break
            except Exception:
                continue

        if not target_user:
            await update.message.reply_text(f"No user found with username @{username_input}")
            return

        revoke_license(target_user.id)
        await update.message.reply_text(
            f"License revoked for @{target_user.username} (ID: {target_user.id})"
        )
    except Exception:
        logger.exception("Error in revoke command")
        await update.message.reply_text("Failed to revoke license. Please try again.")


async def revokelicense(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text("Not authorized.")
        return
    if len(context.args) < 1:
        await update.message.reply_text("Usage: /revokelicense <user_id>")
        return
    try:
        target = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Invalid user ID.")
        return
    revoke_license(target)
    await update.message.reply_text(f"License revoked for user {target}.")


async def viewlogs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text("Not authorized.")
        return

    lines_to_show = 50
    if context.args:
        try:
            lines_to_show = min(max(int(context.args[0]), 10), 500)
        except ValueError:
            pass

    recent_logs = get_recent_logs(lines_to_show)
    if not recent_logs.strip():
        await update.message.reply_text("No log entries found yet.")
        return
    await send_long_text(
        update.message,
        f"Recent activity logs (last {lines_to_show} lines):\n\n{recent_logs}",
    )


async def monitor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text("Not authorized.")
        return
    status_message = format_detailed_status(get_detailed_database_status())
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Refresh", callback_data="refresh_monitor")]]
    )
    await update.message.reply_text(status_message, reply_markup=keyboard)


async def refresh_monitor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("Not authorized.", show_alert=True)
        return
    await query.answer()
    status_message = format_detailed_status(get_detailed_database_status())
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Refresh", callback_data="refresh_monitor")]]
    )
    try:
        await query.edit_message_text(status_message, reply_markup=keyboard)
    except Exception as exc:
        if "Message is not modified" not in str(exc):
            raise


async def serverlogs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text("Not authorized.")
        return
    try:
        if not os.path.exists(MONITOR_LOG_PATH):
            await update.message.reply_text(
                "Monitor log file not found yet. It will be created after the first monitoring cycle."
            )
            return

        entries_to_show = 5
        if context.args:
            try:
                entries_to_show = min(max(int(context.args[0]), 1), 20)
            except ValueError:
                pass

        with LOG_LOCK:
            with open(MONITOR_LOG_PATH, "r", encoding="utf-8") as log_file:
                content = log_file.read()

        entries = [entry.strip() for entry in content.split("=" * 80 + "\n") if entry.strip()]
        recent_entries = entries[-entries_to_show:]
        if not recent_entries:
            await update.message.reply_text("No monitor entries found.")
            return

        output = (
            f"Recent Server Monitor Logs (last {entries_to_show} entries):\n\n"
            + ("\n" + "=" * 60 + "\n\n").join(recent_entries)
        )
        await send_long_text(update.message, output)
    except Exception as exc:
        logger.exception("Error reading monitor logs")
        await update.message.reply_text(f"Error reading monitor logs: {exc}")

# ------------------------------------------------------------
# User Commands
# ------------------------------------------------------------
async def license_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lic = get_license(uid)
    if not lic:
        await update.message.reply_text(
            "Your license has expired, please contact @alite312 to buy a license"
        )
        return

    expires = datetime.fromisoformat(lic["expires_at"])
    now = utcnow()
    if now > expires:
        await update.message.reply_text(
            "You do not have an active license, please contact @alite312 to buy a license"
        )
        return

    days_remaining = max(0, (expires - now).days)
    await update.message.reply_text(
        "Your License Details\n\n"
        f"Days remaining: {days_remaining}\n"
        f"Posts per day: {lic['posts_per_day']}\n"
        f"License expires: {expires.strftime('%Y-%m-%d')}"
    )



async def profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    username = (
        f"@{update.effective_user.username}"
        if update.effective_user.username
        else "No username"
    )

    lic = get_license(uid)

    message = (
        "Your Profile\n\n"
        f"Username: {username}\n"
        "Telegram ID (tap and hold to copy):\n"
        f"<code>{uid}</code>\n\n"
    )

    if not lic:
        message += "License Status: No license"
    else:
        reset_daily_counts_if_needed(uid)
        lic = get_license(uid)

        expires = datetime.fromisoformat(lic["expires_at"])
        now = utcnow()
        is_active = now <= expires
        status = "Active" if is_active else "Expired"
        days_remaining = max(0, (expires - now).days)
        posts_remaining = max(
            0,
            lic["posts_per_day"] - lic["posts_today"],
        )

        message += (
            f"License Status: {status}\n"
            f"License Expires: {expires.strftime('%Y-%m-%d')}\n"
            f"Days Remaining: {days_remaining}\n"
            f"Posts Used Today: {lic['posts_today']}/{lic['posts_per_day']}\n"
            f"Posts Remaining Today: {posts_remaining}"
        )

    await update.message.reply_text(message, parse_mode="HTML")


# ------------------------------------------------------------
# User Flow: /post
# ------------------------------------------------------------
async def post_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    username = update.effective_user.username or "no_username"

    if is_blacklisted(uid):
        await update.message.reply_text("You are blacklisted from using this bot.")
        return

    if not is_admin(uid):
        cooldown = get_cooldown(uid)
        if cooldown and utcnow() < cooldown:
            remaining_seconds = max(0, int((cooldown - utcnow()).total_seconds()))
            minutes, seconds = divmod(remaining_seconds, 60)
            await update.message.reply_text(
                f"You are on cooldown for {minutes} minutes and {seconds} more seconds."
            )
            return

    lic = get_license(uid)
    if not lic:
        await update.message.reply_text("You do not have an active license.")
        return

    expires = datetime.fromisoformat(lic["expires_at"])
    if utcnow() > expires:
        await update.message.reply_text("Your license has expired.")
        return

    reset_daily_counts_if_needed(uid)
    lic = get_license(uid)
    if lic["posts_today"] >= lic["posts_per_day"]:
        await update.message.reply_text("Daily post limit reached.")
        return

    clear_post_flow(context)
    clear_schedule_flow(context)
    log_user_activity(
        uid,
        username,
        "POST ATTEMPT",
        f"Posts today: {lic['posts_today']}/{lic['posts_per_day']}",
    )

    keyboard = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("Yes", callback_data=f"startpost:{uid}"),
            InlineKeyboardButton("No", callback_data=f"cancelpost:{uid}"),
        ]]
    )
    await update.message.reply_text("Begin creating a post?", reply_markup=keyboard)


async def post_start_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data_parts = query.data.split(":")
    if len(data_parts) != 2:
        return

    action, uid_str = data_parts
    uid = int(uid_str)
    if query.from_user.id != uid:
        await query.edit_message_text("This confirmation does not belong to you.")
        return

    username = query.from_user.username or "no_username"
    if action == "cancelpost":
        clear_post_flow(context)
        log_user_activity(uid, username, "POST CANCELLED", "User cancelled before description")
        await query.edit_message_text("Post creation cancelled.")
        return

    context.user_data[USER_AWAITING_DESCRIPTION] = True
    log_user_activity(uid, username, "POST DESCRIPTION STARTED")
    await query.edit_message_text("Send the description of your listing.")


async def receive_description(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    username = update.effective_user.username or "no_username"
    desc = update.message.text.strip()
    validation_error = validate_description(desc)
    if validation_error:
        await update.message.reply_text(validation_error)
        return

    log_user_activity(uid, username, "DESCRIPTION RECEIVED", f"Length: {len(desc)} chars")
    context.user_data[USER_PENDING_DESCRIPTION] = desc
    context.user_data[USER_AWAITING_DESCRIPTION] = False
    context.user_data[USER_AWAITING_IMAGE] = True

    keyboard = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("Add Image", callback_data=f"addimage:{uid}"),
            InlineKeyboardButton("Skip Image", callback_data=f"skipimage:{uid}"),
        ]]
    )
    await update.message.reply_text(
        f"Description received:\n\n{desc}\n\nWould you like to add an image as a banner?",
        reply_markup=keyboard,
    )


async def image_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data_parts = query.data.split(":")
    if len(data_parts) != 2:
        return

    action, uid_str = data_parts
    uid = int(uid_str)
    if query.from_user.id != uid:
        await query.edit_message_text("Not your action.")
        return

    username = query.from_user.username or "no_username"
    if action == "skipimage":
        context.user_data[USER_PENDING_IMAGE] = None
        context.user_data[USER_AWAITING_IMAGE] = False
        desc = context.user_data.get(USER_PENDING_DESCRIPTION)
        log_user_activity(uid, username, "IMAGE SKIPPED")
        await show_final_confirmation(query, desc, None)
        return

    if action == "addimage":
        context.user_data[USER_AWAITING_IMAGE] = True
        log_user_activity(uid, username, "IMAGE REQUESTED")
        await query.edit_message_text("Please send the image you want to use as a banner.")


async def receive_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    username = update.effective_user.username or "no_username"
    if not update.message.photo:
        await update.message.reply_text("Please send a valid image.")
        return

    photo = update.message.photo[-1]
    context.user_data[USER_PENDING_IMAGE] = photo.file_id
    context.user_data[USER_AWAITING_IMAGE] = False
    log_user_activity(uid, username, "IMAGE RECEIVED", f"File ID: {photo.file_id[:20]}...")

    desc = context.user_data.get(USER_PENDING_DESCRIPTION)
    if not desc:
        clear_post_flow(context)
        await update.message.reply_text("No description found. Please start again with /post.")
        return
    await show_final_confirmation(update.message, desc, photo.file_id)


async def show_final_confirmation(message_obj, description: str, image_file_id: Optional[str]):
    uid = message_obj.from_user.id
    text_content = f"Description:\n\n{description}\n\n"
    text_content += "Image included as banner\n\n" if image_file_id else "No image included\n\n"
    text_content += "Please confirm to publish your post:"

    keyboard = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("Confirm", callback_data=f"confirmdesc:{uid}"),
            InlineKeyboardButton("Edit Description", callback_data=f"editdesc:{uid}"),
            InlineKeyboardButton("Cancel", callback_data=f"canceldesc:{uid}"),
        ]]
    )

    if hasattr(message_obj, "edit_message_text"):
        if image_file_id:
            try:
                await message_obj.edit_message_media(
                    media=InputMediaPhoto(media=image_file_id, caption=text_content),
                    reply_markup=keyboard,
                )
            except Exception:
                logger.exception("Error editing message media; sending a new confirmation")
                if message_obj.message:
                    await message_obj.message.reply_photo(
                        photo=image_file_id,
                        caption=text_content,
                        reply_markup=keyboard,
                    )
        else:
            await message_obj.edit_message_text(text_content, reply_markup=keyboard)
    else:
        if image_file_id:
            await message_obj.reply_photo(
                photo=image_file_id,
                caption=text_content,
                reply_markup=keyboard,
            )
        else:
            await message_obj.reply_text(text_content, reply_markup=keyboard)


async def description_actions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data_parts = query.data.split(":")
    if len(data_parts) != 2:
        return

    action, uid_str = data_parts
    uid = int(uid_str)
    if query.from_user.id != uid:
        await query.edit_message_text("Not your action.")
        return

    username = query.from_user.username or "no_username"
    if context.user_data.get(USER_CONFIRMING_POST):
        return
    context.user_data[USER_CONFIRMING_POST] = True

    if action == "editdesc":
        context.user_data[USER_AWAITING_DESCRIPTION] = True
        context.user_data[USER_PENDING_IMAGE] = None
        context.user_data[USER_AWAITING_IMAGE] = False
        context.user_data.pop(USER_CONFIRMING_POST, None)
        log_user_activity(uid, username, "EDIT DESCRIPTION REQUESTED")
        if query.message and query.message.photo:
            await query.edit_message_caption(caption="Send a new description.", reply_markup=None)
        else:
            await query.edit_message_text("Send a new description.", reply_markup=None)
        return

    if action == "canceldesc":
        has_image = bool(query.message and query.message.photo)
        clear_post_flow(context)
        log_user_activity(uid, username, "POST CANCELLED AT CONFIRMATION")
        await edit_result_message(query, "Post creation cancelled.", has_image)
        return

    if action != "confirmdesc":
        context.user_data.pop(USER_CONFIRMING_POST, None)
        return

    desc = context.user_data.get(USER_PENDING_DESCRIPTION)
    if not desc:
        context.user_data.pop(USER_CONFIRMING_POST, None)
        await edit_result_message(
            query,
            "No description found. Please start over with /post.",
            bool(query.message and query.message.photo),
        )
        return

    image_file_id = context.user_data.get(USER_PENDING_IMAGE)
    has_image = image_file_id is not None

    # Re-check the license and quota at the actual publishing moment.
    lic = get_license(uid)
    if not lic or utcnow() > datetime.fromisoformat(lic["expires_at"]):
        clear_post_flow(context)
        await edit_result_message(query, "Your license is no longer active.", has_image)
        return
    reset_daily_counts_if_needed(uid)
    lic = get_license(uid)
    if lic["posts_today"] >= lic["posts_per_day"]:
        clear_post_flow(context)
        await edit_result_message(query, "Daily post limit reached.", has_image)
        return

    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        logger.info("Could not remove confirmation keyboard before publishing")

    footer = (
        f"\n\nPosted by @{query.from_user.username}"
        if query.from_user.username
        else f"\n\nPosted by User {uid}"
    )
    full_description = desc + footer

    try:
        if image_file_id:
            await context.bot.send_photo(
                chat_id=MAIN_CHAT_ID,
                photo=image_file_id,
                caption=full_description,
            )
        else:
            await context.bot.send_message(
                chat_id=MAIN_CHAT_ID,
                text=full_description,
            )
    except Exception as exc:
        logger.exception("Failed to send message to main chat")
        log_user_activity(uid, username, "POST FAILED", str(exc))
        clear_post_flow(context)
        await edit_result_message(
            query,
            "Failed to publish your post. Please try again later.",
            has_image,
        )
        return

    increment_post_count(uid)
    log_post_published(uid, username, full_description, has_image)
    if not is_admin(uid):
        set_cooldown(uid, utcnow() + timedelta(seconds=POST_COOLDOWN_SECONDS))

    clear_post_flow(context)
    await edit_result_message(
        query,
        "Your post has been published to the main channel!",
        has_image,
    )

# ------------------------------------------------------------
# User Flow: /schedule
# ------------------------------------------------------------
async def schedule_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id

    if is_blacklisted(uid):
        await update.message.reply_text("You are blacklisted from using this bot.")
        return

    lic = get_license(uid)
    if not lic:
        await update.message.reply_text("You do not have an active license.")
        return

    expires = datetime.fromisoformat(lic["expires_at"])
    if utcnow() > expires:
        await update.message.reply_text("Your license has expired.")
        return

    reset_daily_counts_if_needed(uid)
    lic = get_license(uid)
    remaining_posts = lic["posts_per_day"] - lic["posts_today"]
    if remaining_posts <= 0:
        await update.message.reply_text("Daily post limit reached. Try again tomorrow.")
        return

    clear_post_flow(context)
    clear_schedule_flow(context)

    max_posts = min(3, remaining_posts)
    if max_posts == 1:
        await update.message.reply_text("You can schedule 1 post right now.")
        context.user_data[USER_SCHEDULE_POST_COUNT] = 1
        context.user_data[USER_SCHEDULE_CURRENT_POST] = 0
        context.user_data[USER_SCHEDULE_POSTS] = []
        await ask_for_schedule_time(update.message, context)
        return

    keyboard = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton(str(index), callback_data=f"schedule_count:{index}:{uid}")
            for index in range(1, max_posts + 1)
        ]]
    )
    await update.message.reply_text(
        f"You have {remaining_posts} posts remaining today.\n"
        f"How many posts would you like to schedule? (up to {max_posts})",
        reply_markup=keyboard,
    )


async def schedule_count_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data_parts = query.data.split(":")
    if len(data_parts) != 3:
        return

    _, count_str, uid_str = data_parts
    uid = int(uid_str)
    count = int(count_str)
    if query.from_user.id != uid:
        await query.edit_message_text("Not your action.")
        return

    lic = get_license(uid)
    if not lic:
        await query.edit_message_text("You do not have an active license.")
        return
    reset_daily_counts_if_needed(uid)
    lic = get_license(uid)
    maximum = min(3, max(0, lic["posts_per_day"] - lic["posts_today"]))
    if count < 1 or count > maximum:
        await query.edit_message_text("That schedule count is no longer available. Use /schedule again.")
        return

    context.user_data[USER_SCHEDULE_POST_COUNT] = count
    context.user_data[USER_SCHEDULE_CURRENT_POST] = 0
    context.user_data[USER_SCHEDULE_POSTS] = []
    await query.edit_message_text(f"Great! You'll schedule {count} posts.")
    await ask_for_schedule_time(query, context)


async def ask_for_schedule_time(message_obj, context: ContextTypes.DEFAULT_TYPE):
    context.user_data[USER_AWAITING_SCHEDULE_TIME] = True
    prompt = (
        "Please send the time when you want the posts to be published.\n\n"
        "Format: HH:MM (24-hour format)\n"
        "Example: 14:30 for 2:30 PM\n\n"
        "Note: Time is in UTC timezone"
    )
    if hasattr(message_obj, "edit_message_text"):
        await message_obj.edit_message_text(prompt)
    else:
        await message_obj.reply_text(prompt)


async def receive_schedule_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    username = update.effective_user.username or "no_username"
    time_str = update.message.text.strip()

    try:
        schedule_time = datetime.strptime(time_str, "%H:%M").time()
    except ValueError:
        await update.message.reply_text(
            "Invalid time format. Please use HH:MM format (24-hour).\n"
            "Example: 14:30 for 2:30 PM"
        )
        return

    now = utcnow()
    scheduled_datetime = datetime.combine(now.date(), schedule_time)
    if scheduled_datetime <= now:
        scheduled_datetime += timedelta(days=1)

    context.user_data[USER_SCHEDULE_TIME] = scheduled_datetime.isoformat()
    context.user_data[USER_AWAITING_SCHEDULE_TIME] = False
    context.user_data[USER_SCHEDULE_AWAITING_DESCRIPTION] = True
    log_user_activity(
        uid,
        username,
        "SCHEDULE TIME RECEIVED",
        f"Time: {scheduled_datetime.isoformat()}",
    )

    current_post = context.user_data.get(USER_SCHEDULE_CURRENT_POST, 0)
    total_posts = context.user_data.get(USER_SCHEDULE_POST_COUNT, 1)
    await update.message.reply_text(
        f"Time set for: {scheduled_datetime.strftime('%Y-%m-%d %H:%M')} UTC\n\n"
        f"Now let's create post {current_post + 1} of {total_posts}.\n"
        "Send the description for this post:"
    )


async def receive_schedule_description(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    username = update.effective_user.username or "no_username"
    desc = update.message.text.strip()
    validation_error = validate_description(desc)
    if validation_error:
        await update.message.reply_text(validation_error)
        return

    current_post = context.user_data.get(USER_SCHEDULE_CURRENT_POST, 0)
    schedule_posts = context.user_data.get(USER_SCHEDULE_POSTS, [])
    if current_post != len(schedule_posts):
        await update.message.reply_text("Schedule state is invalid. Please restart with /schedule.")
        clear_schedule_flow(context)
        return

    schedule_posts.append({"description": desc, "image": None})
    context.user_data[USER_SCHEDULE_POSTS] = schedule_posts
    context.user_data[USER_SCHEDULE_AWAITING_DESCRIPTION] = False
    context.user_data[USER_SCHEDULE_AWAITING_IMAGE] = True

    truncated_desc = desc[:100] + "..." if len(desc) > 100 else desc
    log_user_activity(
        uid,
        username,
        "SCHEDULE DESCRIPTION RECEIVED",
        f"Post {current_post + 1}: {truncated_desc}",
    )

    keyboard = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("Add Image", callback_data=f"schedule_addimage:{uid}"),
            InlineKeyboardButton("Skip Image", callback_data=f"schedule_skipimage:{uid}"),
        ]]
    )
    await update.message.reply_text(
        f"Description for post {current_post + 1} received.\n\n"
        "Would you like to add an image as a banner?",
        reply_markup=keyboard,
    )


async def schedule_image_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data_parts = query.data.split(":")
    if len(data_parts) != 2:
        return

    action, uid_str = data_parts
    uid = int(uid_str)
    if query.from_user.id != uid:
        await query.edit_message_text("Not your action.")
        return

    username = query.from_user.username or "no_username"
    current_post = context.user_data.get(USER_SCHEDULE_CURRENT_POST, 0)

    if action == "schedule_skipimage":
        context.user_data[USER_SCHEDULE_AWAITING_IMAGE] = False
        log_user_activity(uid, username, "SCHEDULE IMAGE SKIPPED", f"Post {current_post + 1}")
        await process_next_scheduled_post(query, context)
        return

    if action == "schedule_addimage":
        context.user_data[USER_SCHEDULE_AWAITING_IMAGE] = True
        log_user_activity(uid, username, "SCHEDULE IMAGE REQUESTED", f"Post {current_post + 1}")
        await query.edit_message_text("Please send the image you want to use as a banner.")


async def receive_schedule_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    username = update.effective_user.username or "no_username"
    current_post = context.user_data.get(USER_SCHEDULE_CURRENT_POST, 0)

    if not update.message.photo:
        await update.message.reply_text("Please send a valid image.")
        return

    schedule_posts = context.user_data.get(USER_SCHEDULE_POSTS, [])
    if current_post >= len(schedule_posts):
        clear_schedule_flow(context)
        await update.message.reply_text("Schedule state is invalid. Please restart with /schedule.")
        return

    photo = update.message.photo[-1]
    schedule_posts[current_post]["image"] = photo.file_id
    context.user_data[USER_SCHEDULE_POSTS] = schedule_posts
    context.user_data[USER_SCHEDULE_AWAITING_IMAGE] = False
    log_user_activity(
        uid,
        username,
        "SCHEDULE IMAGE RECEIVED",
        f"Post {current_post + 1}, File ID: {photo.file_id[:20]}...",
    )
    await process_next_scheduled_post(update.message, context)


async def process_next_scheduled_post(message_obj, context: ContextTypes.DEFAULT_TYPE):
    current_post = context.user_data.get(USER_SCHEDULE_CURRENT_POST, 0)
    total_posts = context.user_data.get(USER_SCHEDULE_POST_COUNT, 1)

    if current_post + 1 >= total_posts:
        await confirm_schedule(message_obj, context)
        return

    context.user_data[USER_SCHEDULE_CURRENT_POST] = current_post + 1
    context.user_data[USER_SCHEDULE_AWAITING_DESCRIPTION] = True
    prompt = (
        f"Now let's create post {current_post + 2} of {total_posts}.\n"
        "Send the description for this post:"
    )
    if hasattr(message_obj, "edit_message_text"):
        await message_obj.edit_message_text(prompt)
    else:
        await message_obj.reply_text(prompt)


async def confirm_schedule(message_obj, context: ContextTypes.DEFAULT_TYPE):
    uid = message_obj.from_user.id
    schedule_posts = context.user_data.get(USER_SCHEDULE_POSTS, [])
    total_posts = len(schedule_posts)
    schedule_time = context.user_data.get(USER_SCHEDULE_TIME)

    if not schedule_time or total_posts == 0:
        clear_schedule_flow(context)
        error_text = "Error: Missing schedule information. Please start over."
        if hasattr(message_obj, "edit_message_text"):
            await message_obj.edit_message_text(error_text)
        else:
            await message_obj.reply_text(error_text)
        return

    scheduled_datetime = datetime.fromisoformat(schedule_time)
    confirmation_text = (
        "Schedule Confirmation\n\n"
        f"Scheduled Time: {scheduled_datetime.strftime('%Y-%m-%d %H:%M')} UTC\n"
        f"Number of Posts: {total_posts}\n\n"
    )
    for index, post in enumerate(schedule_posts, 1):
        description = post["description"]
        preview = description[:100] + ("..." if len(description) > 100 else "")
        confirmation_text += (
            f"Post {index}:\n{preview}\n"
            f"Image: {'Yes' if post['image'] else 'No'}\n\n"
        )
    confirmation_text += "Please confirm to schedule these posts:"

    keyboard = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("Confirm Schedule", callback_data=f"confirm_schedule:{uid}"),
            InlineKeyboardButton("Cancel", callback_data=f"cancel_schedule:{uid}"),
        ]]
    )
    if hasattr(message_obj, "edit_message_text"):
        await message_obj.edit_message_text(confirmation_text, reply_markup=keyboard)
    else:
        await message_obj.reply_text(confirmation_text, reply_markup=keyboard)


async def schedule_confirmation_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data_parts = query.data.split(":")
    if len(data_parts) != 2:
        return

    action, uid_str = data_parts
    uid = int(uid_str)
    if query.from_user.id != uid:
        await query.edit_message_text("Not your action.")
        return

    username = query.from_user.username or "no_username"
    if action == "cancel_schedule":
        clear_schedule_flow(context)
        log_user_activity(uid, username, "SCHEDULE CANCELLED")
        await query.edit_message_text("Schedule cancelled.")
        return

    if action != "confirm_schedule":
        return

    schedule_posts = context.user_data.get(USER_SCHEDULE_POSTS, [])
    total_posts = len(schedule_posts)
    schedule_time = context.user_data.get(USER_SCHEDULE_TIME)
    if not schedule_time or total_posts == 0:
        clear_schedule_flow(context)
        await query.edit_message_text("Error: Missing schedule information. Please start over.")
        return

    lic = get_license(uid)
    if not lic or utcnow() > datetime.fromisoformat(lic["expires_at"]):
        clear_schedule_flow(context)
        await query.edit_message_text("Your license is no longer active.")
        return
    reset_daily_counts_if_needed(uid)
    lic = get_license(uid)
    remaining = max(0, lic["posts_per_day"] - lic["posts_today"])
    if total_posts > remaining:
        clear_schedule_flow(context)
        await query.edit_message_text(
            "You no longer have enough posts remaining today for this schedule. Use /schedule again."
        )
        return

    scheduled_datetime = datetime.fromisoformat(schedule_time)
    try:
        for index, post in enumerate(schedule_posts):
            add_scheduled_post(
                user_id=uid,
                username=username,
                description=post["description"],
                image_file_id=post["image"],
                scheduled_time=schedule_time,
                post_index=index,
                total_posts=total_posts,
            )
    except Exception:
        logger.exception("Failed to save scheduled posts")
        await query.edit_message_text("Failed to save the schedule. Please try again.")
        return

    log_scheduled_post(uid, username, schedule_time, total_posts)
    clear_schedule_flow(context)
    await query.edit_message_text(
        "Schedule Confirmed!\n\n"
        f"Number of Posts: {total_posts}\n"
        f"Scheduled Time: {scheduled_datetime.strftime('%Y-%m-%d %H:%M')} UTC\n\n"
        "Your posts will be automatically published at the specified time.\n"
        "You will be notified when all posts are published."
    )


async def myschedule_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    scheduled_posts = get_user_scheduled_posts(uid)
    if not scheduled_posts:
        await update.message.reply_text("You have no scheduled posts.")
        return

    posts_by_time = {}
    for post in scheduled_posts:
        post_id, description, scheduled_time, post_index, total_posts, status = post
        posts_by_time.setdefault(scheduled_time, []).append(
            {
                "id": post_id,
                "description": description,
                "post_index": post_index,
                "total_posts": total_posts,
                "status": status,
            }
        )

    message = "Your Scheduled Posts\n\n"
    now = utcnow()
    for schedule_time, posts in posts_by_time.items():
        scheduled_datetime = datetime.fromisoformat(schedule_time)
        posts.sort(key=lambda item: item["post_index"])

        if scheduled_datetime <= now:
            time_status = "Time reached"
        else:
            time_remaining = scheduled_datetime - now
            total_minutes = int(time_remaining.total_seconds() // 60)
            days, leftover_minutes = divmod(total_minutes, 1440)
            hours, minutes = divmod(leftover_minutes, 60)
            prefix = f"{days}d " if days else ""
            time_status = f"In {prefix}{hours}h {minutes}m"

        message += (
            f"Schedule Time: {scheduled_datetime.strftime('%Y-%m-%d %H:%M')} UTC\n"
            f"Status: {time_status}\n"
            f"Posts: {len(posts)} scheduled\n\n"
        )
        for display_index, post in enumerate(posts, 1):
            description = post["description"]
            preview = description[:50] + ("..." if len(description) > 50 else "")
            message += (
                f"  {display_index}. ID {post['id']} | Post "
                f"{post['post_index'] + 1}/{post['total_posts']} | "
                f"{post['status']}: {preview}\n"
            )
        message += "\n"

    message += "Use /deleteschedule <id> to delete a scheduled post."
    await send_long_text(update.message, message)


async def deleteschedule_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if len(context.args) < 1:
        await update.message.reply_text(
            "Usage: /deleteschedule <post_id>\n\nGet post IDs from /myschedule command."
        )
        return
    try:
        post_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Invalid post ID. Please provide a numeric ID.")
        return

    if delete_scheduled_post(post_id, uid):
        await update.message.reply_text("Scheduled post deleted successfully.")
    else:
        await update.message.reply_text(
            "Post not found or you don't have permission to delete it."
        )


# ------------------------------------------------------------
# Message Routers
# ------------------------------------------------------------
async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # One text handler prevents the same update from being consumed by both
    # the normal post flow and the schedule flow.
    if context.user_data.get(USER_AWAITING_SCHEDULE_TIME):
        await receive_schedule_time(update, context)
    elif context.user_data.get(USER_SCHEDULE_AWAITING_DESCRIPTION):
        await receive_schedule_description(update, context)
    elif context.user_data.get(USER_AWAITING_DESCRIPTION):
        await receive_description(update, context)


async def photo_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get(USER_SCHEDULE_AWAITING_IMAGE):
        await receive_schedule_image(update, context)
    elif context.user_data.get(USER_AWAITING_IMAGE):
        await receive_image(update, context)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Unhandled exception while processing update", exc_info=context.error)


# ------------------------------------------------------------
# Application Setup
# ------------------------------------------------------------
def initialize_log_files():
    if not os.path.exists(USER_LOG_PATH):
        with open(USER_LOG_PATH, "w", encoding="utf-8") as log_file:
            log_file.write("=" * 80 + "\n")
            log_file.write("MARKETPLACE BOT USER ACTIVITY LOG\n")
            log_file.write(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            log_file.write("=" * 80 + "\n\n")

    if not os.path.exists(MONITOR_LOG_PATH):
        with open(MONITOR_LOG_PATH, "w", encoding="utf-8") as monitor_file:
            monitor_file.write("=" * 80 + "\n")
            monitor_file.write("SERVER MONITOR LOG\n")
            monitor_file.write(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            monitor_file.write(f"Monitoring Interval: {MONITOR_INTERVAL} seconds\n")
            monitor_file.write("=" * 80 + "\n\n")


def main():
    init_db()
    initialize_log_files()

    app = ApplicationBuilder().token(TOKEN).build()

    # Basic commands
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("post", post_command))
    app.add_handler(CommandHandler("license", license_command))
    app.add_handler(CommandHandler("profile", profile_command))
    app.add_handler(CommandHandler("schedule", schedule_command))
    app.add_handler(CommandHandler("myschedule", myschedule_command))
    app.add_handler(CommandHandler("deleteschedule", deleteschedule_command))

    # Admin commands
    app.add_handler(CommandHandler("newlicense", newlicense_command))
    app.add_handler(CommandHandler("blacklist", blacklist))
    app.add_handler(CommandHandler("unblacklist", unblacklist))
    app.add_handler(CommandHandler("licenses", licenses_cmd))
    app.add_handler(CommandHandler("remaining", remaining))
    app.add_handler(CommandHandler("revoke", revoke_command))
    app.add_handler(CommandHandler("revokelicense", revokelicense))
    app.add_handler(CommandHandler("viewlogs", viewlogs))
    app.add_handler(CommandHandler("monitor", monitor))
    app.add_handler(CommandHandler("serverlogs", serverlogs))

    # Monitoring callback
    app.add_handler(CallbackQueryHandler(refresh_monitor, pattern=r"^refresh_monitor$"))

    # Normal post callbacks
    app.add_handler(
        CallbackQueryHandler(post_start_confirm, pattern=r"^(startpost|cancelpost):")
    )
    app.add_handler(
        CallbackQueryHandler(image_choice, pattern=r"^(addimage|skipimage):")
    )
    app.add_handler(
        CallbackQueryHandler(
            description_actions,
            pattern=r"^(confirmdesc|editdesc|canceldesc):",
        )
    )

    # Schedule callbacks
    app.add_handler(
        CallbackQueryHandler(schedule_count_callback, pattern=r"^schedule_count:")
    )
    app.add_handler(
        CallbackQueryHandler(
            schedule_image_choice,
            pattern=r"^(schedule_addimage|schedule_skipimage):",
        )
    )
    app.add_handler(
        CallbackQueryHandler(
            schedule_confirmation_callback,
            pattern=r"^(confirm_schedule|cancel_schedule):",
        )
    )

    # A single router for each content type avoids state collisions.
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))
    app.add_handler(MessageHandler(filters.PHOTO, photo_router))
    app.add_error_handler(error_handler)

    start_monitoring()

    job_queue = app.job_queue
    if job_queue is None:
        raise RuntimeError(
            'JobQueue is unavailable. Install with: pip install "python-telegram-bot[job-queue]"'
        )
    job_queue.run_repeating(
        check_scheduled_posts,
        interval=SCHEDULE_CHECK_INTERVAL,
        first=10,
    )
    logger.info(
        "Scheduled posts checker started (interval: %ss)",
        SCHEDULE_CHECK_INTERVAL,
    )

    logger.info("Bot starting with server monitoring and schedule features enabled...")
    app.run_polling()


if __name__ == "__main__":
    main()
