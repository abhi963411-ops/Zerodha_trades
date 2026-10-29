
import os
import threading
import time
from html import escape
from pathlib import Path

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse
from kiteconnect import KiteConnect


# --------------------------------------------------
# 1. Project paths and credentials
# --------------------------------------------------

PROJECT_DIR = Path(__file__).resolve().parent

load_dotenv(PROJECT_DIR / ".env")

API_KEY = os.getenv("KITE_API_KEY")
API_SECRET = os.getenv("KITE_API_SECRET")

TOKEN_FILE = PROJECT_DIR / "access_token.txt"

HOST = "127.0.0.1"
PORT = 8000
CALLBACK_URL = f"http://{HOST}:{PORT}/callback"


if not API_KEY or not API_SECRET:
    raise RuntimeError(
        "KITE_API_KEY or KITE_API_SECRET is missing from .env"
    )


# --------------------------------------------------
# 2. Create one shared KiteConnect client
# --------------------------------------------------

kite = KiteConnect(api_key=API_KEY)

# Load an existing token if the file exists.
# Its validity will be verified by an API request.
if TOKEN_FILE.is_file():
    saved_token = TOKEN_FILE.read_text(encoding="utf-8").strip()

    if saved_token:
        kite.set_access_token(saved_token)


# --------------------------------------------------
# 3. Create the callback web application
# --------------------------------------------------

app = FastAPI(
    title="Kite Connect Authentication",
    docs_url=None,
    redoc_url=None,
)

_server = None
_server_thread = None
_server_lock = threading.Lock()
_shutdown_timer = None


# --------------------------------------------------
# 4. Reusable dark-themed browser page
# --------------------------------------------------

def render_page(title: str, message: str, success: bool) -> str:
    safe_title = escape(title)
    safe_message = escape(message)

    status_color = "#34d399" if success else "#fb7185"
    status_label = "AUTHENTICATED" if success else "ATTENTION REQUIRED"

    return f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>{safe_title} | Kite Connect</title>
        <style>
            * {{ box-sizing: border-box; }}

            body {{
                margin: 0;
                min-height: 100vh;
                display: grid;
                place-items: center;
                padding: 24px;
                background:
                    radial-gradient(ellipse at top, #172554 0%, transparent 55%),
                    #080d18;
                color: #e5e7eb;
                font-family: Inter, Segoe UI, Arial, sans-serif;
            }}

            .card {{
                width: 100%;
                max-width: 500px;
                padding: 38px;
                border: 1px solid #293449;
                border-radius: 22px;
                background: rgba(15, 23, 42, 0.94);
                box-shadow: 0 25px 80px rgba(0, 0, 0, 0.35);
            }}

            .brand {{
                color: #93c5fd;
                font-size: 13px;
                font-weight: 700;
                letter-spacing: 2px;
                text-transform: uppercase;
            }}

            .status {{
                display: inline-block;
                margin-top: 30px;
                padding: 7px 11px;
                border: 1px solid {status_color};
                border-radius: 999px;
                color: {status_color};
                font-size: 11px;
                font-weight: 700;
                letter-spacing: 1px;
            }}

            h1 {{
                margin: 18px 0 12px;
                font-size: clamp(27px, 6vw, 36px);
                letter-spacing: -1px;
            }}

            p {{
                color: #aab6c8;
                font-size: 15px;
                line-height: 1.8;
                overflow-wrap: anywhere;
            }}

            .note {{
                margin-top: 28px;
                padding-top: 18px;
                border-top: 1px solid #293449;
                font-size: 12px;
            }}

            .dot {{
                display: inline-block;
                width: 8px;
                height: 8px;
                margin-right: 8px;
                border-radius: 50%;
                background: {status_color};
            }}
        </style>
    </head>
    <body>
        <main class="card">
            <div class="brand">Kite Connect · Local Session</div>
            <div class="status">{status_label}</div>
            <h1>{safe_title}</h1>
            <p>{safe_message}</p>
            <p class="note">
                <span class="dot"></span>
                Your API credentials and access token are not displayed here.
            </p>
        </main>
    </body>
    </html>
    """


# --------------------------------------------------
# 5. Landing page
# --------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def home():
    return HTMLResponse(
        render_page(
            "Authentication service is ready",
            "This local service handles the Zerodha login callback. "
            "Start the callback server before opening the Kite login URL.",
            True,
        )
    )


# --------------------------------------------------
# 6. Zerodha redirects the browser to this endpoint
# --------------------------------------------------

@app.get("/callback", response_class=HTMLResponse)
def callback(
    request_token: str | None = Query(default=None),
    status: str | None = Query(default=None),
):
    if status == "error":
        return HTMLResponse(
            render_page(
                "Login was not completed",
                "Zerodha reported a login error. Return to your notebook "
                "and start a new login attempt.",
                False,
            ),
            status_code=400,
        )

    if not request_token:
        return HTMLResponse(
            render_page(
                "Request token missing",
                "The callback did not contain a request token. "
                "Start a new login attempt from your notebook.",
                False,
            ),
            status_code=400,
        )

    try:
        # Exchange the short-lived request token for an access token.
        session = kite.generate_session(
            request_token,
            api_secret=API_SECRET,
        )

        access_token = session["access_token"]

        # Update the shared client so notebook API calls can use it.
        kite.set_access_token(access_token)

        # Save only the access token, never the API secret.
        TOKEN_FILE.write_text(access_token, encoding="utf-8")

        schedule_server_stop(2)

        return HTMLResponse(
            render_page(
                "Login successful",
                "Your Kite Connect session has been established. "
                "Return to your notebook and test it with kite.profile().",
                True,
            )
        )

    except Exception:
        # Do not expose exception details or credentials in the browser.
        return HTMLResponse(
            render_page(
                "Authentication failed",
                "Zerodha could not complete the session exchange. "
                "The request token may have expired or already been used. "
                "Check your notebook and try a fresh login.",
                False,
            ),
            status_code=400,
        )


# --------------------------------------------------
# 7. Explicitly start the callback server
# --------------------------------------------------


def stop_callback_server():
    """Request server shutdown and wait briefly for it to stop."""
    global _shutdown_timer

    with _server_lock:
        if _shutdown_timer is not None:
            _shutdown_timer.cancel()
            _shutdown_timer = None

        server = _server
        server_thread = _server_thread

        if server is None or server_thread is None:
            print("Callback server is not running.")
            return

        if not server_thread.is_alive():
            print("Callback server has already stopped.")
            return

        server.should_exit = True

    # Wait outside the lock so the server thread can finish.
    if threading.current_thread() is not server_thread:
        server_thread.join(timeout=5)

    if not server_thread.is_alive():
        print("Callback server stopped.")
    else:
        print("Shutdown requested. The server is still stopping.")


def schedule_server_stop(seconds):
    """Stop the callback server after the specified number of seconds."""
    global _shutdown_timer

    with _server_lock:
        if _shutdown_timer is not None:
            _shutdown_timer.cancel()

        _shutdown_timer = threading.Timer(
            seconds, stop_callback_server
        )
        _shutdown_timer.daemon = True
        _shutdown_timer.start()


def start_callback_server():
    """Start the callback server and schedule automatic shutdown."""
    global _server, _server_thread

    with _server_lock:
        if _server_thread is not None and _server_thread.is_alive():
            print("Callback server is already running.")
            return

        config = uvicorn.Config(
            app,
            host=HOST,
            port=PORT,
            log_level="info",
            access_log=True,
        )

        _server = uvicorn.Server(config)
        _server_thread = threading.Thread(
            target=_server.run,
            daemon=True,
            name="kite-callback-server",
        )
        _server_thread.start()

    # Automatically stop after 2 minutes.
    schedule_server_stop(120)

    print(f"Callback server started at http://{HOST}:{PORT}")
    print("It will stop automatically after 120 seconds.")
