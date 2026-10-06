import logging, threading
from datetime import datetime
from zoneinfo import ZoneInfo
import requests
from curl_cffi import requests as cffi_requests
from .config import settings
from .db import add_restock_log, claim_schedule_run, list_enabled_webhooks, load_event_state, upsert_event_state
from .security import decrypt_webhook

log = logging.getLogger("48group.monitor")
EVENTS = [
    {"group":"JKT48","api_url":"https://jkt48.com/api/v1/exclusives/EX5B99/bonus?lang=id","buy_url":"https://jkt48.com/purchase/exclusive?code=EX5B99"},
    {"group":"AKB48","api_url":"https://jkt48.com/api/v1/exclusives/EXD1A1/bonus?lang=id","buy_url":"https://jkt48.com/purchase/exclusive?code=EXD1A1"},
]
COLOR_GREEN=0x2ECC71; COLOR_RED=0xE74C3C; COLOR_BLUE=0x3498DB; COLOR_PURPLE=0x9B59B6
STATE_LOCK=threading.Lock()
RUNTIME_STATE={"members":[],"last_check":None,"last_success":None,"last_error":None,"running":False}

def fetch_api(url):
    try:
        r=cffi_requests.get(url, impersonate="chrome", timeout=15)
        if r.status_code==200: return r.json()
        raise RuntimeError(f"API HTTP {r.status_code}")
    except Exception as exc:
        log.warning("API fetch gagal %s: %s", url, exc); return None

def parse_api_data(response_json, group_name, buy_url):
    parsed=[]
    if not response_json: return parsed
    for session_obj in response_json.get("data",[]):
        if not isinstance(session_obj,dict): continue
        session_name=session_obj.get("label","-")
        for detail in session_obj.get("session_members",[]):
            if not isinstance(detail,dict): continue
            track=detail.get("label","-"); member_name=detail.get("member_name","Unknown")
            try: stock=int(detail.get("available_quota",0) or 0)
            except (TypeError,ValueError): stock=0
            uid=f"{group_name}_{member_name}_{session_name}_{track}"
            parsed.append({"id":uid,"group":group_name,"name":member_name,"session":session_name,"track":track,"quota":stock>0,"stock":stock,"buy_url":buy_url})
    return parsed

def get_all_members_data():
    members=[]; success=0
    for event in EVENTS:
        data=fetch_api(event["api_url"])
        if data is not None:
            success += 1; members.extend(parse_api_data(data,event["group"],event["buy_url"]))
    return members, success

def discord_post(webhook_url: str, payload: dict):
    headers = {
        "User-Agent": "48Group-2Shot-Monitor/1.0",
        "Content-Type": "application/json",
    }

    try:
        response = requests.post(
            webhook_url,
            params={"wait": "true"},
            json=payload,
            headers=headers,
            timeout=15,
        )
    except requests.RequestException as exc:
        raise RuntimeError(
            f"Gagal terhubung ke Discord: {type(exc).__name__}: {exc}"
        ) from exc

    if response.status_code not in (200, 204):
        raise RuntimeError(
            f"Discord HTTP {response.status_code}: {response.text[:500]}"
        )

    return response

def send_embed(webhook_url,title,description,color,content=None,fields=None):
    embed={"title":title,"description":description,"color":color,"footer":{"text":"48Group 2-Shot Monitor • Automatic System"},"timestamp":datetime.utcnow().isoformat()+"Z"}
    if fields: embed["fields"]=fields
    payload={"embeds":[embed]}
    if content:
        payload["content"]=content
        payload["allowed_mentions"]={"parse":["everyone"]}
    discord_post(webhook_url,payload)

def send_activation_message(webhook_url):
    send_embed(webhook_url,"✅ 48GROUP MONITOR ACTIVATED","Webhook berhasil terhubung.\n\n• **Restock alert:** realtime\n• **Daily report:** 08:00 / 12:00 / 20:00 WIB\n• **Monitoring:** JKT48 2-Shot & AKB48 2-Shot\n\nSistem monitoring sekarang aktif.",COLOR_GREEN)

def send_test_message(webhook_url): send_embed(webhook_url,"🧪 TEST WEBHOOK BERHASIL","Dashboard berhasil mengirim pesan ke channel Discord ini.",COLOR_GREEN)

def _broadcast_restock(item,old_stock):
    description=(f"> 🏢 **Grup:** `{item['group']}`\n> 👤 **Member:** `{item['name']}`\n> 🕒 **Sesi:** `{item['session']}`\n> 📍 **Jalur:** `{item['track']}`\n> 📦 **Stok:** `{old_stock} → {item['stock']}`\n\n👉 **[BELI TIKET 2-SHOT]({item['buy_url']})**")
    for hook in list_enabled_webhooks(item["group"]):
        try:
            url=decrypt_webhook(hook["webhook_url_enc"])
            content="@everyone 🚨 **RESTOCK TERDETEKSI!**" if hook["mention_everyone"] else "🚨 **RESTOCK TERDETEKSI!**"
            send_embed(url,"🚨 2-SHOT RESTOCK ALERT!",description,COLOR_BLUE,content=content)
        except Exception: log.exception("Gagal kirim restock webhook id=%s",hook["id"])

def _available_group_text(members,group_name,limit=18):
    available=[m for m in members if m["group"]==group_name and m["stock"]>0]
    if not available: return "❌ Seluruh slot sedang sold out."
    lines=[f"• **{m['name']}** — `{m['session']}` | `{m['track']}` → **{m['stock']}**" for m in available[:limit]]
    if len(available)>limit: lines.append(f"…dan {len(available)-limit} slot tersedia lainnya.")
    return "\n".join(lines)[:1000]

def send_scheduled_report(members,report_hour):
    jkt=_available_group_text(members,"JKT48"); akb=_available_group_text(members,"AKB48")
    for hook in list_enabled_webhooks():
        try:
            fields=[]
            if hook["notify_jkt"]: fields.append({"name":"🏢 JKT48","value":jkt,"inline":False})
            if hook["notify_akb"]: fields.append({"name":"🏢 AKB48","value":akb,"inline":False})
            send_embed(decrypt_webhook(hook["webhook_url_enc"]),f"📊 REKAP 2-SHOT • {report_hour:02d}:00 WIB","Status ketersediaan terbaru dari monitor 48Group.",COLOR_PURPLE,fields=fields)
        except Exception: log.exception("Gagal kirim report webhook id=%s",hook["id"])

def snapshot():
    with STATE_LOCK: return {k:(list(v) if k=="members" else v) for k,v in RUNTIME_STATE.items()}

class MonitorService:
    def __init__(self):
        self.stop_event=threading.Event(); self.thread=None; self.prev_state={}; self.last_schedule_key=None
    def start(self):
        if self.thread and self.thread.is_alive(): return
        self.prev_state=load_event_state(); self.stop_event.clear(); self.thread=threading.Thread(target=self.run,daemon=True,name="48group-monitor"); self.thread.start()
    def stop(self):
        self.stop_event.set()
        if self.thread: self.thread.join(timeout=10)
    def run(self):
        with STATE_LOCK: RUNTIME_STATE["running"]=True
        log.info("Monitor dimulai; interval=%ss",settings.check_interval)
        while not self.stop_event.is_set():
            try:
                self.poll_once(); self.maybe_send_scheduled_report()
            except Exception as exc:
                log.exception("Monitor loop error")
                with STATE_LOCK: RUNTIME_STATE["last_error"]=str(exc)
            self.stop_event.wait(settings.check_interval)
        with STATE_LOCK: RUNTIME_STATE["running"]=False
    def poll_once(self):
        now=datetime.now(ZoneInfo(settings.timezone))
        with STATE_LOCK: RUNTIME_STATE["last_check"]=now.isoformat()
        members,success=get_all_members_data()
        if success==0:
            with STATE_LOCK: RUNTIME_STATE["last_error"]="Semua endpoint API gagal diakses."
            return
        current={m["id"]:m for m in members}
        if not self.prev_state:
            for item in members: upsert_event_state(item)
        else:
            for uid,item in current.items():
                previous=self.prev_state.get(uid)
                if previous is not None:
                    old_stock=int(previous.get("stock",0))
                    if old_stock<=0 and item["stock"]>0:
                        log.info("RESTOCK %s %s %s %s: %s -> %s",item["group"],item["name"],item["session"],item["track"],old_stock,item["stock"])
                        add_restock_log(item,old_stock,item["stock"]); _broadcast_restock(item,old_stock)
                upsert_event_state(item)
        merged_state = dict(self.prev_state)
        merged_state.update({i["id"]:{"stock":i["stock"]} for i in members})
        self.prev_state = merged_state
        with STATE_LOCK:
            RUNTIME_STATE["members"]=members; RUNTIME_STATE["last_success"]=now.isoformat(); RUNTIME_STATE["last_error"]=None
    def maybe_send_scheduled_report(self):
        now=datetime.now(ZoneInfo(settings.timezone))
        if now.hour not in (8,12,20) or now.minute>=2: return
        key=f"{now.date().isoformat()}-{now.hour}"
        if self.last_schedule_key==key: return
        members=snapshot()["members"]
        if members and claim_schedule_run(key):
            send_scheduled_report(members,now.hour); self.last_schedule_key=key; log.info("Scheduled report %02d:00 WIB terkirim",now.hour)
