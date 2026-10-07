import hmac
import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import Body, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import settings
from .db import (
    add_member_target,
    add_webhook,
    create_user,
    delete_member_target,
    delete_webhook,
    get_user,
    get_user_by_username,
    get_webhook_for_user,
    init_db,
    list_member_targets,
    list_mng_weekly_status,
    list_user_webhooks,
    recent_restocks,
    toggle_webhook,
)
from .monitor import (
    MonitorService,
    build_mng_weekly_board,
    mng_week_key,
    send_activation_message,
    send_test_message,
    snapshot,
)
from .show_feature import (
    init_show_db,
    router as show_router,
    show_health,
    start_show_service,
    stop_show_service,
)
from .security import (
    decrypt_webhook,
    encrypt_webhook,
    hash_password,
    make_session_token,
    mask_webhook,
    read_session_token,
    verify_password,
)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
)
logger = logging.getLogger("48group.main")

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

WEBHOOK_RE = re.compile(
    r"^https://(?:canary\.|ptb\.)?"
    r"(?:discord\.com|discordapp\.com)"
    r"/api/webhooks/\d+/[A-Za-z0-9._-]+/?$"
)

BUY_URLS = {
    "JKT48": "https://jkt48.com/purchase/exclusive?code=EX5B99",
    "AKB48": "https://jkt48.com/purchase/exclusive?code=EXD1A1",
    "JKT48_MNG": "https://jkt48.com/purchase/exclusive?code=EX24AE",
}

DISPLAY_NAMES = {
    "JKT48": "JKT48 2-Shot",
    "AKB48": "AKB48 2-Shot",
    "JKT48_MNG": "JKT48 M&G",
}

service = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global service

    if not settings.app_secret:
        raise RuntimeError("APP_SECRET wajib di-set.")
    if not settings.webhook_encryption_key:
        raise RuntimeError("WEBHOOK_ENCRYPTION_KEY wajib di-set.")
    if not settings.collector_secret:
        raise RuntimeError("COLLECTOR_SECRET wajib di-set.")

    init_db()
    init_show_db()

    service = MonitorService()
    service.start()
    start_show_service()

    logger.info("48Group Monitor started in LOCAL COLLECTOR mode.")

    try:
        yield
    finally:
        stop_show_service()

        if service:
            service.stop()

        logger.info("48Group Monitor stopped.")


app = FastAPI(title="48Group Ticket Monitor", lifespan=lifespan)
app.include_router(show_router)
app.mount(
    "/static",
    StaticFiles(directory=str(BASE_DIR / "static")),
    name="static",
)


def current_user(request: Request):
    token = request.cookies.get("session")
    if not token:
        return None

    user_id = read_session_token(token)
    if not user_id:
        return None

    return get_user(user_id)


def go_login():
    return RedirectResponse("/login", status_code=303)


def _collector_token(request: Request):
    authorization = request.headers.get("Authorization", "")
    if authorization.startswith("Bearer "):
        return authorization[len("Bearer ") :].strip()

    return request.headers.get("X-Collector-Token", "").strip()


@app.get("/health")
def health():
    state = snapshot()
    return JSONResponse(
        {
            "ok": True,
            "monitor_running": state["running"],
            "mode": "local_collector",
            "collector_last_seen": state.get("collector_last_seen"),
            "last_check": state["last_check"],
            "last_success": state["last_success"],
            "last_error": state["last_error"],
            "groups": state["groups"],
            "show_collector": show_health(),
        }
    )


@app.post("/api/collector/snapshot")
def collector_snapshot(request: Request, payload: dict = Body(...)):
    token = _collector_token(request)

    if not token or not hmac.compare_digest(token, settings.collector_secret):
        raise HTTPException(status_code=401, detail="Invalid collector token.")

    if service is None:
        raise HTTPException(status_code=503, detail="Monitor service belum siap.")

    try:
        return JSONResponse(service.ingest_snapshot(payload))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Collector snapshot gagal diproses.")
        raise HTTPException(
            status_code=500,
            detail=f"{type(exc).__name__}: {exc}",
        ) from exc


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    user = current_user(request)
    if not user:
        return go_login()

    state = snapshot()
    members = state["members"]
    available = [m for m in members if m["stock"] > 0]

    def stats(group_name):
        group_members = [m for m in members if m["group"] == group_name]
        available_members = [m for m in group_members if m["stock"] > 0]
        return {
            "total": len(group_members),
            "available": len(available_members),
            "sold_out": max(0, len(group_members) - len(available_members)),
        }

    hooks = []
    for row in list_user_webhooks(user["id"]):
        hook = dict(row)
        try:
            hook["masked_url"] = mask_webhook(
                decrypt_webhook(row["webhook_url_enc"])
            )
        except Exception:
            hook["masked_url"] = "[encryption key mismatch]"
        hooks.append(hook)

    # Dropdown/autocomplete member list from the current collector state.
    member_options = sorted(
        {
            (m["group"], m["name"])
            for m in members
            if m.get("group") in BUY_URLS and m.get("name")
        },
        key=lambda item: (item[0], item[1].casefold()),
    )

    raw_targets = [dict(row) for row in list_member_targets(user["id"])]
    sniping_targets = []
    target_keys = set()

    for target in raw_targets:
        key = (target["group_name"], target["member_name"])
        target_keys.add(key)

        target_slots = [
            m
            for m in members
            if m["group"] == target["group_name"]
            and m["name"] == target["member_name"]
            and m["stock"] > 0
        ]

        target["available_slots"] = len(target_slots)
        target["available_stock"] = sum(m["stock"] for m in target_slots)
        target["is_available"] = bool(target_slots)
        target["buy_url"] = BUY_URLS.get(target["group_name"], "#")
        sniping_targets.append(target)

    restocks = []
    for row in recent_restocks(15):
        item = dict(row)
        item["buy_url"] = BUY_URLS.get(item["group_name"], "#")
        item["is_target"] = (
            item["group_name"],
            item["member_name"],
        ) in target_keys
        restocks.append(item)

    current_mng_week = mng_week_key()
    mng_qualification_rows = [
        dict(row)
        for row in list_mng_weekly_status(current_mng_week)
    ]
    mng_weekly_board = build_mng_weekly_board(
        members,
        mng_qualification_rows,
    )

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "user": user,
            "state": state,
            "available": available,
            "jkt": stats("JKT48"),
            "akb": stats("AKB48"),
            "mng": stats("JKT48_MNG"),
            "display_names": DISPLAY_NAMES,
            "mng_week_key": current_mng_week,
            "mng_weekly_board": mng_weekly_board,
            "mng_cutoff_captured": bool(mng_qualification_rows),
            "webhooks": hooks,
            "restocks": restocks,
            "member_options": member_options,
            "sniping_targets": sniping_targets,
            "target_keys": target_keys,
            "message": request.query_params.get("message"),
            "error": request.query_params.get("error"),
        },
    )


@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="register.html",
        context={"error": None},
    )


@app.post("/register")
def register(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    username = username.strip()

    if len(username) < 3:
        return templates.TemplateResponse(
            request=request,
            name="register.html",
            context={"error": "Username minimal 3 karakter."},
            status_code=400,
        )

    if len(password) < 8:
        return templates.TemplateResponse(
            request=request,
            name="register.html",
            context={"error": "Password minimal 8 karakter."},
            status_code=400,
        )

    if get_user_by_username(username):
        return templates.TemplateResponse(
            request=request,
            name="register.html",
            context={"error": "Username sudah digunakan."},
            status_code=400,
        )

    try:
        user_id = create_user(username, hash_password(password))
    except Exception:
        logger.exception("Gagal membuat akun.")
        return templates.TemplateResponse(
            request=request,
            name="register.html",
            context={"error": "Gagal membuat akun."},
            status_code=400,
        )

    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        "session",
        make_session_token(user_id),
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        max_age=60 * 60 * 24 * 30,
    )
    return response


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"error": None},
    )


@app.post("/login")
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    user = get_user_by_username(username.strip())

    if not user or not verify_password(password, user["password_hash"]):
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"error": "Username atau password salah."},
            status_code=401,
        )

    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        "session",
        make_session_token(user["id"]),
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        max_age=60 * 60 * 24 * 30,
    )
    return response


@app.post("/logout")
def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie("session")
    return response


@app.post("/sniping")
def add_sniping_target_route(
    request: Request,
    target: str = Form(...),
):
    user = current_user(request)
    if not user:
        return go_login()

    try:
        group_name, member_name = target.split("|", 1)
    except ValueError:
        return RedirectResponse(
            "/?error=" + quote("Pilihan member sniping tidak valid."),
            status_code=303,
        )

    group_name = group_name.strip()
    member_name = member_name.strip()

    if group_name not in BUY_URLS or not member_name:
        return RedirectResponse(
            "/?error=" + quote("Pilihan member sniping tidak valid."),
            status_code=303,
        )

    state = snapshot()
    valid_members = {
        (m["group"], m["name"])
        for m in state["members"]
        if m.get("group") in BUY_URLS and m.get("name")
    }

    if valid_members and (group_name, member_name) not in valid_members:
        return RedirectResponse(
            "/?error=" + quote("Member tidak ditemukan pada snapshot terbaru."),
            status_code=303,
        )

    created = add_member_target(user["id"], group_name, member_name)
    message = (
        f"🎯 {group_name} • {member_name} ditambahkan ke daftar sniping."
        if created
        else f"{group_name} • {member_name} sudah ada di daftar sniping."
    )

    return RedirectResponse(
        "/?message=" + quote(message),
        status_code=303,
    )


@app.post("/sniping/{target_id}/delete")
def delete_sniping_target_route(target_id: int, request: Request):
    user = current_user(request)
    if not user:
        return go_login()

    delete_member_target(target_id, user["id"])
    return RedirectResponse(
        "/?message=" + quote("Target sniping dihapus."),
        status_code=303,
    )


@app.post("/webhooks")
def create_webhook_route(
    request: Request,
    name: str = Form(...),
    webhook_url: str = Form(...),
    notify_jkt: str | None = Form(None),
    notify_akb: str | None = Form(None),
    notify_mng: str | None = Form(None),
):
    user = current_user(request)
    if not user:
        return go_login()

    webhook_url = webhook_url.strip().rstrip("/")
    if not WEBHOOK_RE.match(webhook_url):
        return RedirectResponse(
            "/?error=" + quote("Format URL webhook Discord tidak valid."),
            status_code=303,
        )

    use_jkt = notify_jkt == "on"
    use_akb = notify_akb == "on"
    use_mng = notify_mng == "on"

    if not use_jkt and not use_akb and not use_mng:
        return RedirectResponse(
            "/?error=" + quote("Pilih minimal satu notifikasi: JKT48 2-Shot, AKB48 2-Shot, atau JKT48 M&G."),
            status_code=303,
        )

    try:
        send_activation_message(webhook_url)
        add_webhook(
            user["id"],
            name.strip() or "Discord Channel",
            encrypt_webhook(webhook_url),
            use_jkt,
            use_akb,
            use_mng,
            True,
        )
        logger.info("Webhook berhasil ditambahkan oleh user id=%s", user["id"])
        return RedirectResponse(
            "/?message="
            + quote(
                "Webhook berhasil diaktifkan. Restock akan langsung mengirim @everyone"
            ),
            status_code=303,
        )
    except Exception as exc:
        logger.exception("Gagal menambahkan webhook")
        error_message = str(exc)
        if len(error_message) > 350:
            error_message = error_message[:350] + "..."
        return RedirectResponse(
            "/?error=" + quote(error_message),
            status_code=303,
        )


@app.post("/webhooks/{webhook_id}/test")
def test_webhook_route(webhook_id: int, request: Request):
    user = current_user(request)
    if not user:
        return go_login()

    hook = get_webhook_for_user(webhook_id, user["id"])
    if not hook:
        return RedirectResponse(
            "/?error=" + quote("Webhook tidak ditemukan."),
            status_code=303,
        )

    try:
        send_test_message(decrypt_webhook(hook["webhook_url_enc"]))
        return RedirectResponse(
            "/?message=" + quote("Test webhook berhasil."),
            status_code=303,
        )
    except Exception as exc:
        logger.exception("Test webhook gagal")
        error_message = str(exc)
        if len(error_message) > 350:
            error_message = error_message[:350] + "..."
        return RedirectResponse(
            "/?error=" + quote("Test webhook gagal: " + error_message),
            status_code=303,
        )


@app.post("/webhooks/{webhook_id}/toggle")
def toggle_webhook_route(webhook_id: int, request: Request):
    user = current_user(request)
    if not user:
        return go_login()

    hook = get_webhook_for_user(webhook_id, user["id"])
    if not hook:
        return RedirectResponse(
            "/?error=" + quote("Webhook tidak ditemukan."),
            status_code=303,
        )

    toggle_webhook(webhook_id, user["id"])
    return RedirectResponse(
        "/?message=" + quote("Status webhook diperbarui."),
        status_code=303,
    )


@app.post("/webhooks/{webhook_id}/delete")
def delete_webhook_route(webhook_id: int, request: Request):
    user = current_user(request)
    if not user:
        return go_login()

    hook = get_webhook_for_user(webhook_id, user["id"])
    if not hook:
        return RedirectResponse(
            "/?error=" + quote("Webhook tidak ditemukan."),
            status_code=303,
        )

    delete_webhook(webhook_id, user["id"])
    return RedirectResponse(
        "/?message=" + quote("Webhook berhasil dihapus."),
        status_code=303,
    )
