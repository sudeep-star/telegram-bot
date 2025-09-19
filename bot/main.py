import os
import asyncio
import logging
from dataclasses import dataclass, field
from typing import Dict, Optional, List, Tuple, Any, Union
from datetime import datetime
from urllib.parse import urlparse, urlencode

import json
import re

import aiohttp
from aiohttp import ClientConnectorError, ClientSSLError, ClientResponseError
from dotenv import load_dotenv

from telegram import Update, Bot, BotCommand
from telegram.ext import Application, CommandHandler, ContextTypes
# === NEW: imports for mention handling ===
from telegram.ext import MessageHandler, filters
from telegram.constants import MessageEntityType

# ============================
# Config & Env
# ============================
load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GROUP_CHAT_ID = os.getenv("GROUP_CHAT_ID")  # e.g. "-1001234567890"
TOPIC_ID = os.getenv("TELEGRAM_TOPIC_ID")   # optional: for forum topics
if not TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is required")
if not GROUP_CHAT_ID:
    raise RuntimeError("GROUP_CHAT_ID is required")

# Intervals (seconds)
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", 3600))          # summary cadence (default 1h)
FAST_CHECK_INTERVAL = int(os.getenv("FAST_CHECK_INTERVAL", 60))  # probe cadence for instant alerts
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", 10))        # per-request timeout

# Flap control
FAILURES_TO_MARK_DOWN = int(os.getenv("FAILURES_TO_MARK_DOWN", 2))  # consecutive fails to mark DOWN
SUCCESSES_TO_MARK_UP = int(os.getenv("SUCCESSES_TO_MARK_UP", 1))    # consecutive successes to mark UP

# Probe retry
PROBE_RETRIES = int(os.getenv("PROBE_RETRIES", 1))
PROBE_RETRY_BACKOFF = float(os.getenv("PROBE_RETRY_BACKOFF", 0.5))

# Optional custom UA
USER_AGENT = os.getenv("USER_AGENT", "MokshyaUptimeBot/1.0 (+https://mokshya.ai)")

# History config
MAX_HISTORY_PER_SITE = int(os.getenv("MAX_HISTORY_PER_SITE", 50))       # ring buffer per site
HISTORY_DEFAULT_LIMIT = int(os.getenv("HISTORY_DEFAULT_LIMIT", 20))      # default rows returned

# Websites list (supports comma or newline separated)
_raw_sites = os.getenv("WEBSITES")
if _raw_sites:
    WEBSITES = [s.strip() for s in _raw_sites.replace(",", "\n").splitlines() if s.strip()]
else:
    WEBSITES = [
        "https://staging.mokshya.ai/",
        "https://mokshya.ai/",
        "https://wapal.io/",
        "https://mokshya.io/",
        "https://staging.alura.fun/",
        "https://alura.fun/",
        "https://gudne.com.np/",
        "https://nassec.io/",
    ]

# ============================
# Logging
# ============================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

# ============================
# Models & State
# ============================
@dataclass
class ProbeResult:
    ok: bool
    status: Optional[int] = None
    err: Optional[str] = None
    latency_ms: Optional[int] = None

@dataclass
class SiteState:
    url: str
    is_up: Optional[bool] = None
    first_seen: datetime = field(default_factory=datetime.now)
    last_change: datetime = field(default_factory=datetime.now)
    down_since: Optional[datetime] = None
    consec_failures: int = 0
    consec_successes: int = 0
    last_status_code: Optional[int] = None
    last_error: Optional[str] = None
    last_latency_ms: Optional[int] = None

    # Stats (since process start)
    total_checks: int = 0
    total_ups: int = 0
    total_downs: int = 0

    # State-change history (newest last); each item is (timestamp, event_string)
    history: List[Tuple[datetime, str]] = field(default_factory=list)

states: Dict[str, SiteState] = {u: SiteState(url=u) for u in WEBSITES}

# --- API monitoring models/state + env expansion
@dataclass
class ApiCheck:
    name: str
    url: str
    method: str = "GET"
    headers: Dict[str, str] = field(default_factory=dict)
    # body can be dict/list (JSON) or str (raw/form)
    body: Optional[Union[Dict[str, Any], List[Any], str]] = None

@dataclass
class ApiState(SiteState):
    name: str = ""

# Env placeholder expansion: ${VAR}
_ENV_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)\}")

def _expand_env_in_str(s: str) -> str:
    def repl(m):
        return os.getenv(m.group(1), "")
    return _ENV_PATTERN.sub(repl, s)

def _expand_env(obj: Any) -> Any:
    if isinstance(obj, str):
        return _expand_env_in_str(obj)
    if isinstance(obj, dict):
        return {k: _expand_env(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_expand_env(v) for v in obj]
    return obj

API_CHECKS: List[ApiCheck] = []
api_states: Dict[str, ApiState] = {}

# Summary timing & modes
last_summary_sent = datetime.now()  # adjusted after first probe
first_probe_done = False
initial_summary_sent = False
quiet_mode = False  # suppress periodic summaries when True

# === NEW: cache bot username for mention detection ===
BOT_USERNAME: Optional[str] = None

# ============================
# Helpers
# ============================
def hostname_from_url(url: str) -> str:
    try:
        return urlparse(url).netloc or url
    except Exception:
        return url

def fmt_uptime_ratio(s: SiteState) -> str:
    if s.total_checks == 0:
        return "n/a"
    pct = (s.total_ups / s.total_checks) * 100.0
    return f"{pct:.1f}%"

def status_emoji(is_up: bool) -> str:
    return "✅" if is_up else "❌"

def fmt_latency(s: SiteState) -> str:
    return f"{s.last_latency_ms}ms" if s.last_latency_ms is not None else "—"

def fmt_down_reason(s: SiteState) -> str:
    if s.last_error:
        return s.last_error
    if s.last_status_code is not None:
        return f"HTTP {s.last_status_code}"
    return "Unknown error"

def fmt_since(dt: Optional[datetime]) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else "unknown"

def add_history(s: SiteState, event: str) -> None:
    """Append an event to site's history, trimming to MAX_HISTORY_PER_SITE."""
    s.history.append((datetime.now(), event))
    if len(s.history) > MAX_HISTORY_PER_SITE:
        s.history[:] = s.history[-MAX_HISTORY_PER_SITE:]

def normalize_url(url: str) -> Optional[str]:
    """Ensure URL starts with http(s) and has a hostname."""
    url = url.strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        return None
    try:
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            return None
        return url
    except Exception:
        return None

# === NEW: mention detection helper ===
def is_bot_mentioned(msg_text: Optional[str], entities, bot_username: Optional[str]) -> bool:
    """Return True if @bot_username is explicitly mentioned in the message text/entities."""
    if not msg_text or not bot_username:
        return False

    at_username = f"@{bot_username.lower()}"
    text_l = msg_text.lower()

    # Fast path: raw text contains @username
    if at_username in text_l:
        return True

    # Safer path: check message entities for mentions directed at the bot
    try:
        for ent in (entities or []):
            if ent.type == MessageEntityType.MENTION:
                mentioned = msg_text[ent.offset: ent.offset + ent.length].lower()
                if mentioned == at_username:
                    return True
            elif ent.type == MessageEntityType.TEXT_MENTION and getattr(ent, "user", None):
                if ent.user.username and ent.user.username.lower() == bot_username.lower():
                    return True
    except Exception:
        pass

    return False

# ============================
# Load API checks (file + env fallback)
# ============================
def _load_api_checks_from_file(path: str) -> List[ApiCheck]:
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logging.error(f"Failed to read {path}: {e}")
        return []
    checks: List[ApiCheck] = []
    for c in data:
        try:
            c = _expand_env(c)
            checks.append(ApiCheck(
                name=c["name"],
                url=c["url"],
                method=c.get("method", "GET").upper(),
                headers=c.get("headers") or {},
                body=c.get("body"),
            ))
        except Exception as e:
            logging.error(f"Invalid item in {path}: {e}")
    return checks

def _load_api_checks_from_env_list() -> List[ApiCheck]:
    raw = os.getenv("API_CHECKS", "")
    if not raw.strip():
        return []
    try:
        data = json.loads(raw)
    except Exception as e:
        logging.error(f"Failed to parse API_CHECKS env JSON: {e}")
        return []
    checks: List[ApiCheck] = []
    for c in data:
        try:
            c = _expand_env(c)
            checks.append(ApiCheck(
                name=c["name"],
                url=c["url"],
                method=c.get("method", "GET").upper(),
                headers=c.get("headers") or {},
                body=c.get("body"),
            ))
        except Exception as e:
            logging.error(f"Invalid API_CHECKS item: {e}")
    return checks

def load_api_checks() -> List[ApiCheck]:
    path = os.getenv("API_CHECKS_FILE", "api_checks.json")
    checks = _load_api_checks_from_file(path)
    if checks:
        logging.info(f"Loaded {len(checks)} API checks from {path}")
        return checks
    env_checks = _load_api_checks_from_env_list()
    if env_checks:
        logging.info(f"Loaded {len(env_checks)} API checks from API_CHECKS env")
    return env_checks

def rebuild_api_state(checks: List[ApiCheck]) -> None:
    """Rebuild api_states dict (preserve history when possible)."""
    global API_CHECKS, api_states
    old_states = api_states
    API_CHECKS = checks
    new_states: Dict[str, ApiState] = {}
    for chk in API_CHECKS:
        if chk.name in old_states:
            new_states[chk.name] = old_states[chk.name]
        else:
            new_states[chk.name] = ApiState(url=chk.url, name=chk.name)
    api_states = new_states

# ============================
# Telegram helpers
# ============================
async def send_telegram(bot: Bot, message: str) -> None:
    """Send a message to the configured group (and optional topic) with retry."""
    retries = 3
    for attempt in range(retries):
        try:
            if TOPIC_ID:
                await bot.send_message(
                    chat_id=GROUP_CHAT_ID,
                    text=message,
                    message_thread_id=int(TOPIC_ID)
                )
            else:
                await bot.send_message(chat_id=GROUP_CHAT_ID, text=message)
            return
        except Exception as e:
            logging.error(f"Telegram send failed ({attempt+1}/{retries}): {e}")
            if attempt < retries - 1:
                await asyncio.sleep(2)

# ============================
# HTTP probing (websites)
# ============================
async def single_request(session: aiohttp.ClientSession, method: str, url: str) -> ProbeResult:
    start = datetime.now()
    try:
        async with session.request(method, url, timeout=REQUEST_TIMEOUT, allow_redirects=True) as resp:
            ok = 200 <= resp.status < 400
            latency = int((datetime.now() - start).total_seconds() * 1000)
            return ProbeResult(ok=ok, status=resp.status, latency_ms=latency)
    except asyncio.TimeoutError:
        return ProbeResult(ok=False, err="Timeout")
    except ClientSSLError:
        return ProbeResult(ok=False, err="SSL error")
    except ClientConnectorError as e:
        return ProbeResult(ok=False, err=f"Connect error: {e.os_error or e}")
    except ClientResponseError as e:
        return ProbeResult(ok=False, status=e.status, err=f"Resp error: {e.message}")
    except Exception as e:
        return ProbeResult(ok=False, err=f"Error: {e}")

async def probe(session: aiohttp.ClientSession, url: str) -> ProbeResult:
    """HEAD first, then GET; tiny internal retry for resiliency."""
    methods = [("HEAD",), ("GET",)]
    attempt = 0
    last: ProbeResult = ProbeResult(ok=False, err="no-attempt")
    while attempt <= PROBE_RETRIES:
        for (m,) in methods:
            last = await single_request(session, m, url)
            if last.ok:
                return last
        attempt += 1
        if attempt <= PROBE_RETRIES:
            await asyncio.sleep(PROBE_RETRY_BACKOFF)
    return last

# ============================
# API probing (simplified: request completes => UP)
# ============================
async def api_probe(session: aiohttp.ClientSession, check: ApiCheck) -> ProbeResult:
    """
    Simple health:
      - If the API responds at all (any HTTP status, any body) => mark UP
      - If it errors or times out => mark DOWN
    """
    start = datetime.now()
    ct = (check.headers.get("Content-Type") or "").lower() if check.headers else ""
    send_json = None
    send_data = None

    try:
        # Encode body depending on type
        if isinstance(check.body, (dict, list)):
            if "application/x-www-form-urlencoded" in ct and isinstance(check.body, dict):
                send_data = urlencode(check.body, doseq=True)
            else:
                send_json = check.body
        elif isinstance(check.body, str):
            send_data = _expand_env_in_str(check.body)

        async with session.request(
            check.method,
            check.url,
            headers=check.headers or None,
            json=send_json,
            data=send_data,
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True
        ) as resp:
            await resp.read()  # ensure body can be read
            latency = int((datetime.now() - start).total_seconds() * 1000)
            return ProbeResult(ok=True, status=resp.status, latency_ms=latency)

    except asyncio.TimeoutError:
        return ProbeResult(ok=False, err="Timeout")
    except ClientSSLError:
        return ProbeResult(ok=False, err="SSL error")
    except ClientConnectorError as e:
        return ProbeResult(ok=False, err=f"Connect error: {e.os_error or e}")
    except ClientResponseError as e:
        return ProbeResult(ok=False, status=e.status, err=f"Resp error: {e.message}")
    except Exception as e:
        return ProbeResult(ok=False, err=f"Error: {e}")

# ============================
# State update & summaries
# ============================
def update_state_with_probe(state: SiteState, result: ProbeResult) -> List[str]:
    """Update counters/state; return immediate alert messages (if any)."""
    msgs: List[str] = []
    state.total_checks += 1

    if result.ok:
        state.consec_successes += 1
        state.consec_failures = 0
        state.last_status_code = result.status
        state.last_error = None
        state.last_latency_ms = result.latency_ms
        state.total_ups += 1

        should_mark_up = (state.is_up is False and state.consec_successes >= SUCCESSES_TO_MARK_UP) or state.is_up is None
        if should_mark_up:
            state.is_up = True
            state.last_change = datetime.now()
            evt = f"UP ({result.status}, {fmt_latency(state)})"
            add_history(state, evt)
            state.down_since = None
            label = getattr(state, "name", None) or state.url  # APIs show name; websites show URL
            msgs.append(f"✅ {label} is UP. ({result.status}, {fmt_latency(state)})")
    else:
        state.consec_failures += 1
        state.consec_successes = 0
        state.last_status_code = result.status
        state.last_error = result.err
        state.last_latency_ms = result.latency_ms
        state.total_downs += 1

        if (state.is_up is True or state.is_up is None) and state.consec_failures >= FAILURES_TO_MARK_DOWN:
            state.is_up = False
            state.last_change = datetime.now()
            if state.down_since is None:
                state.down_since = state.last_change
            evt = f"DOWN ({fmt_down_reason(state)})"
            add_history(state, evt)
            label = getattr(state, "name", None) or state.url
            msgs.append(f"❌ {label} is DOWN. ({fmt_down_reason(state)})")

    return msgs

# --- REPLACED: tick to include APIs
async def tick(session: aiohttp.ClientSession) -> List[str]:
    web_tasks = [probe(session, url) for url in WEBSITES]
    api_tasks = [api_probe(session, chk) for chk in API_CHECKS]

    web_results = await asyncio.gather(*web_tasks)
    api_results = await asyncio.gather(*api_tasks)

    immediate_msgs: List[str] = []
    # websites
    for url, res in zip(WEBSITES, web_results):
        immediate_msgs.extend(update_state_with_probe(states[url], res))
    # apis
    for chk, res in zip(API_CHECKS, api_results):
        immediate_msgs.extend(update_state_with_probe(api_states[chk.name], res))
    return immediate_msgs

def build_summary() -> str:
    lines = ["🕒 Status summary", datetime.now().strftime("%Y-%m-%d %H:%M:%S")]

    # Websites
    for url in WEBSITES:
        s = states[url]
        if s.is_up is None:
            status = "↔️ unknown"
            latency = ""
            reason = ""
        else:
            status = f"{status_emoji(s.is_up)} {'UP' if s.is_up else 'DOWN'}"
            latency = f" – {fmt_latency(s)}" if s.is_up else ""
            reason = "" if s.is_up else f" – {fmt_down_reason(s)}"
        uptime = fmt_uptime_ratio(s)
        lines.append(f"{status} {url}{latency}{reason}  |  uptime: {uptime}")

    # APIs (names only; hide URLs)
    if api_states:
        lines.append("\n🧩 APIs")
        for name, s in api_states.items():
            if s.is_up is None:
                status = "↔️ unknown"; tail = ""
            else:
                status = f"{status_emoji(s.is_up)} {'UP' if s.is_up else 'DOWN'}"
                tail = f" – {fmt_latency(s)}" if s.is_up else f" – {fmt_down_reason(s)}"
            uptime = fmt_uptime_ratio(s)
            lines.append(f"{status} {name}{tail}  |  uptime: {uptime}")

    lines.append("")
    lines.append(f"🔕 Quiet mode: {'ON' if quiet_mode else 'OFF'}")
    return "\n".join(lines)

def build_up_list() -> str:
    if not first_probe_done:
        return "⏳ Checks haven't completed yet. Try again in a moment."
    up_sites = [s for s in states.values() if s.is_up]
    up_apis = [s for s in api_states.values() if s.is_up]
    if not up_sites and not up_apis:
        return "⚠️ Nothing is currently UP."
    lines = ["✅ *Currently UP:*"]
    for s in up_sites:
        lines.append(f"• {s.url} – {fmt_latency(s)} | uptime: {fmt_uptime_ratio(s)}")
    for s in up_apis:
        lines.append(f"• {s.name} – {fmt_latency(s)} | uptime: {fmt_uptime_ratio(s)}")
    return "\n".join(lines)

def build_down_list() -> str:
    if not first_probe_done:
        return "⏳ Checks haven't completed yet. Try again in a moment."
    down_sites = [s for s in states.values() if s.is_up is False]
    down_apis = [s for s in api_states.values() if s.is_up is False]
    if not down_sites and not down_apis:
        return "🎉 All monitored targets are UP right now."
    lines = ["❌ *Currently DOWN:*"]
    for s in down_sites:
        lines.append(f"• {s.url} – {fmt_down_reason(s)} (since {fmt_since(s.down_since)})")
    for s in down_apis:
        lines.append(f"• {s.name} – {fmt_down_reason(s)} (since {fmt_since(s.down_since)})")
    return "\n".join(lines)

def build_history_list(filter_text: Optional[str], limit: int) -> str:
    if not first_probe_done:
        return "⏳ Checks haven't completed yet. Try again in a moment."

    entries: List[Tuple[datetime, str, str]] = []  # (ts, label, event)

    # websites
    for url, s in states.items():
        if filter_text:
            ft = filter_text.lower()
            if (ft not in url.lower()) and (ft not in hostname_from_url(url).lower()):
                continue
        for ts, evt in s.history:
            entries.append((ts, url, evt))

    # apis (names only)
    for name, s in api_states.items():
        if filter_text:
            ft = filter_text.lower()
            if (ft not in name.lower()):
                continue
        for ts, evt in s.history:
            entries.append((ts, name, evt))

    if not entries:
        if filter_text:
            return f"ℹ️ No history found for '{filter_text}'."
        return "ℹ️ No history to show yet."

    entries.sort(key=lambda x: x[0], reverse=True)
    entries = entries[:max(1, limit)]

    lines = []
    header = "🧾 *Recent history*"
    if filter_text:
        header += f" (filter: `{filter_text}`)"
    header += f" — showing last {len(entries)}"
    lines.append(header)

    for ts, label, evt in entries:
        lines.append(f"[{ts.strftime('%Y-%m-%d %H:%M:%S')}] {evt} — {label}")

    return "\n".join(lines)

# ============================
# Monitoring loop (background task)
# ============================
async def monitor(bot: Bot):
    global last_summary_sent, first_probe_done, initial_summary_sent, quiet_mode

    timeout = aiohttp.ClientTimeout(total=None, connect=REQUEST_TIMEOUT)
    connector = aiohttp.TCPConnector(ssl=False)  # set True if you require strict SSL
    headers = {"User-Agent": USER_AGENT}

    async with aiohttp.ClientSession(timeout=timeout, connector=connector, headers=headers) as session:
        logging.info("Monitoring started…")
        while True:
            immediate_msgs = await tick(session)

            if not first_probe_done:
                first_probe_done = True

            if immediate_msgs:
                await send_telegram(bot, "\n".join(immediate_msgs))

            now = datetime.now()

            # Initial summary (only if not quiet)
            if first_probe_done and not initial_summary_sent:
                if not quiet_mode:
                    await send_telegram(bot, build_summary())
                initial_summary_sent = True
                last_summary_sent = now
            # Periodic summaries (only if not quiet)
            elif first_probe_done and (now - last_summary_sent).total_seconds() >= CHECK_INTERVAL:
                if not quiet_mode:
                    await send_telegram(bot, build_summary())
                last_summary_sent = now

            await asyncio.sleep(FAST_CHECK_INTERVAL)

# ============================
# Commands
# ============================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info("/start invoked by %s", update.effective_user.id if update.effective_user else "unknown")
    msg = (
        "👋 Hello! I'm your Website & API Monitoring Bot.\n\n"
        "I will:\n"
        "• Continuously check the configured websites and APIs.\n"
        "• Send alerts when a target goes ❌ DOWN or comes ✅ UP.\n"
        "• Post periodic summaries.\n\n"
        "Use /help to see available commands."
    )
    await update.message.reply_text(msg)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info("/help invoked")
    msg = (
        "📖 *Available commands:*\n\n"
        "/start – Introduction\n"
        "/help – Show this help message\n"
        "/status – Live summary of websites + APIs\n"
        "/up – List only the targets currently UP\n"
        "/down – List only the targets currently DOWN\n"
        "/history [filter] – Recent UP/DOWN events; optionally filter by domain/name\n"
        "/quiet – Disable periodic summaries (keep alerts)\n"
        "/loud – Re-enable periodic summaries\n"
        "/add <url> – Add a new website\n"
        "/remove <url|substring> – Remove a website by exact URL or unique substring\n"
        "/apis – API-only summary\n"
        "/reloadapis – Reload api_checks.json without restart"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info("/status invoked")
    await update.message.reply_text(build_summary())

async def up_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info("/up invoked")
    await update.message.reply_text(build_up_list(), parse_mode="Markdown")

async def down_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info("/down invoked")
    await update.message.reply_text(build_down_list(), parse_mode="Markdown")

async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info("/history invoked with args=%s", context.args)
    filter_text = " ".join(context.args).strip() if context.args else None
    msg = build_history_list(filter_text=filter_text, limit=HISTORY_DEFAULT_LIMIT)
    await update.message.reply_text(msg, parse_mode="Markdown")

# Quiet/Loud
async def quiet_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info("/quiet invoked")
    global quiet_mode
    quiet_mode = True
    await update.message.reply_text("🔕 Periodic summaries have been *disabled*.\nImmediate alerts will still be sent.", parse_mode="Markdown")

async def loud_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info("/loud invoked")
    global quiet_mode
    quiet_mode = False
    await update.message.reply_text("🔔 Periodic summaries have been *enabled* again.", parse_mode="Markdown")

# Add / Remove (websites)
async def add_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info("/add invoked with args=%s", context.args)
    if not context.args:
        return await update.message.reply_text("⚠️ Usage: /add <url>")
    url_raw = context.args[0]
    url = normalize_url(url_raw)
    if not url:
        return await update.message.reply_text("⚠️ URL must start with http:// or https:// and include a valid hostname.")
    if url in states:
        return await update.message.reply_text(f"ℹ️ {url} is already being monitored.")

    states[url] = SiteState(url=url)
    WEBSITES.append(url)
    await update.message.reply_text(f"➕ Added {url} to monitoring list.\nIt will be probed on the next cycle.")
    logging.info("Added new site: %s", url)

async def remove_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info("/remove invoked with args=%s", context.args)
    if not context.args:
        return await update.message.reply_text("⚠️ Usage: /remove <url|substring>")

    token = context.args[0].strip()
    url_norm = normalize_url(token)

    # Exact URL case
    if url_norm and url_norm in states:
        del states[url_norm]
        try:
            WEBSITES.remove(url_norm)
        except ValueError:
            pass
        await update.message.reply_text(f"🗑️ Removed {url_norm} from monitoring.")
        logging.info("Removed site (exact): %s", url_norm)
        return

    # Substring search across URLs and hostnames
    ft = token.lower()
    matches = []
    for u in list(states.keys()):
        if ft in u.lower() or ft in hostname_from_url(u).lower():
            matches.append(u)

    if len(matches) == 0:
        return await update.message.reply_text(f"ℹ️ No monitored site matched '{token}'.")
    if len(matches) > 1:
        sample = "\n".join(f"• {m}" for m in matches[:10])
        more = "" if len(matches) <= 10 else f"\n… and {len(matches)-10} more"
        return await update.message.reply_text(
            f"⚠️ Multiple matches for '{token}'. Please be specific or provide full URL:\n{sample}{more}"
        )

    # Unique match: remove it
    target = matches[0]
    del states[target]
    try:
        WEBSITES.remove(target)
    except ValueError:
        pass
    await update.message.reply_text(f"🗑️ Removed {target} from monitoring.")
    logging.info("Removed site (by substring): %s", target)

# APIs: list + reload
async def apis_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not api_states:
        return await update.message.reply_text("ℹ️ No API checks configured.")
    lines = ["🧩 *APIs*"]
    for s in api_states.values():
        if s.is_up is None:
            lines.append(f"• {s.name} – ↔️ unknown")
        elif s.is_up:
            lines.append(f"• {s.name} – ✅ UP – {fmt_latency(s)} | uptime: {fmt_uptime_ratio(s)}")
        else:
            lines.append(f"• {s.name} – ❌ DOWN – {fmt_down_reason(s)} | uptime: {fmt_uptime_ratio(s)}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def reloadapis_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    path = os.getenv("API_CHECKS_FILE", "api_checks.json")
    checks = load_api_checks()
    rebuild_api_state(checks)
    await update.message.reply_text(f"🔄 Reloaded API checks from {path}. Total: {len(API_CHECKS)}")

# === NEW: Mention handler ===
async def mention_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BOT_USERNAME
    msg = update.effective_message
    if not msg:
        return

    if is_bot_mentioned(msg.text or "", msg.entities, BOT_USERNAME):
        reply = (
            "👋 Hey! How can I help?\n"
            "Try:\n"
            "• /status – live summary\n"
            "• /up – currently UP\n"
            "• /down – currently DOWN\n"
            "• /apis – API-only summary\n"
            "• /history keyword – filter history"
        )
        await msg.reply_text(reply)

# ============================
# Error handler (logs exceptions)
# ============================
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logging.exception("Exception while handling an update: %s", context.error)

# ============================
# App startup hook to launch monitor
# and REGISTER COMMANDS so they SHOW in menu
# ============================
async def post_init_start_monitor(app: Application):
    global BOT_USERNAME

    commands = [
        BotCommand("start", "Introduction"),
        BotCommand("help", "Show help"),
        BotCommand("status", "Live summary"),
        BotCommand("up", "List UP"),
        BotCommand("down", "List DOWN"),
        BotCommand("history", "Recent events"),
        BotCommand("quiet", "Disable summaries"),
        BotCommand("loud", "Enable summaries"),
        BotCommand("add", "Add website"),
        BotCommand("remove", "Remove website"),
        BotCommand("apis", "API summary"),
        BotCommand("reloadapis", "Reload api_checks.json"),
    ]
    try:
        await app.bot.set_my_commands(commands)
        me = await app.bot.get_me()  # === NEW: learn username
        BOT_USERNAME = me.username
        logging.info("Bot commands registered. Username: @%s", BOT_USERNAME)
    except Exception as e:
        logging.error("Failed to set bot commands or get me(): %s", e)

    # Load API checks on startup
    rebuild_api_state(load_api_checks())

    asyncio.create_task(monitor(app.bot))
    logging.info("Background monitor task started.")

# ============================
# Main (non-async) — PTB manages the loop
# ============================
def main():
    application = Application.builder().token(TOKEN).build()

    # === NEW: mention handler for any text message (non-command) ===
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, mention_handler))

    # Handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("up", up_command))
    application.add_handler(CommandHandler("down", down_command))
    application.add_handler(CommandHandler("history", history_command))
    application.add_handler(CommandHandler("quiet", quiet_command))
    application.add_handler(CommandHandler("loud", loud_command))
    application.add_handler(CommandHandler("add", add_command))
    application.add_handler(CommandHandler("remove", remove_command))

    # API handlers
    application.add_handler(CommandHandler("apis", apis_command))
    application.add_handler(CommandHandler("reloadapis", reloadapis_command))

    # Errors
    application.add_error_handler(error_handler)

    # Launch monitor after init (also registers commands, caches username)
    application.post_init = post_init_start_monitor

    # Let PTB manage the event loop
    application.run_polling()

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logging.info("Shutting down…")
