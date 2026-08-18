# -----------------------------------------------------------------------------
# 1. ライブラリのインポート
# -----------------------------------------------------------------------------
import json
import socket
import sqlite3
import time
import uuid
from typing import List, Optional
from datetime import datetime, time as time_obj, date

import httpx
import uvicorn
from fastapi import FastAPI, Depends, HTTPException, status, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

# APScheduler
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.job import Job
from apscheduler.jobstores.base import JobLookupError

# -----------------------------------------------------------------------------
# 2. データベースとスケジューラーの設定
# -----------------------------------------------------------------------------
DATABASE_FILE = "ir_database.db"
JOBSTORE_FILE = "jobs.db"

def get_server_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80)); ip = s.getsockname()[0]
    except Exception: ip = "127.0.0.1"
    finally: s.close()
    return ip

SERVER_IP = get_server_ip()
SERVER_PORT = 8102
print(f"INFO:     Server IP detected as: {SERVER_IP}")

def init_db():
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute("CREATE TABLE IF NOT EXISTS ir_signals (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, raw_data TEXT NOT NULL)")
    conn.commit(); conn.close()
    print("Database initialized.")

jobstores = {'default': SQLAlchemyJobStore(url=f'sqlite:///{JOBSTORE_FILE}')}
scheduler = BackgroundScheduler(jobstores=jobstores, timezone="Asia/Tokyo")

# -----------------------------------------------------------------------------
# 3. IR信号送信のコア機能
# -----------------------------------------------------------------------------
def execute_ir_send(signal_name: str, esp32_ip: str):
    print(f"Executing send for '{signal_name}' to {esp32_ip}")
    conn = sqlite3.connect(DATABASE_FILE); conn.row_factory = sqlite3.Row
    try:
        cursor = conn.cursor(); cursor.execute("SELECT raw_data FROM ir_signals WHERE name = ?", (signal_name,))
        db_signal = cursor.fetchone()
        if db_signal is None:
            print(f"Error: Signal '{signal_name}' not found for job."); return
        raw_data_list = json.loads(db_signal["raw_data"])
        esp32_payload = {"format": "raw", "freq": 38, "data": raw_data_list}
        esp32_url = f"http://{esp32_ip}/ir/send"
        with httpx.Client() as client: client.post(esp32_url, json=esp32_payload, timeout=5.0).raise_for_status()
        print(f"Successfully sent '{signal_name}' to {esp32_ip}.")
    except (httpx.RequestError, httpx.HTTPStatusError) as e:
        print(f"Error sending '{signal_name}' to {esp32_ip}. Reason: {e}")
    finally:
        conn.close()

def execute_wakeup_alarm(on_signal: str, off_signal: str, interval_sec: float, duration_sec: int, esp32_ip: str):
    print(f"Starting wakeup alarm: ON='{on_signal}', OFF='{off_signal}', duration={duration_sec}s")
    start_time = time.time()
    while time.time() - start_time < duration_sec:
        execute_ir_send(on_signal, esp32_ip)
        time.sleep(interval_sec)
        if time.time() - start_time >= duration_sec: break
        execute_ir_send(off_signal, esp32_ip)
        time.sleep(interval_sec)
    print(f"Wakeup alarm finished for '{on_signal}/{off_signal}'.")

# -----------------------------------------------------------------------------
# 4. Pydanticモデル
# -----------------------------------------------------------------------------
class SignalBase(BaseModel): name: str
class SignalListResponse(SignalBase): id: int
class SignalResponse(SignalListResponse): raw_data: List[int]
class SignalCreate(SignalBase): raw_data: List[int]
class SignalUpdate(BaseModel): name: Optional[str] = None; raw_data: Optional[List[int]] = None
class SendRequest(BaseModel): esp32_ip: str
class ScheduleBase(SendRequest):
    execute_time: time_obj; execute_date: Optional[date] = None
    repeat_type: str; repeat_days: Optional[List[str]] = None
class ScheduleRequest(ScheduleBase): name: str
class WakeupScheduleRequest(ScheduleBase):
    on_signal_name: str; off_signal_name: str
    interval_seconds: float = Field(..., gt=0); duration_seconds: int = Field(..., gt=0)
class ReceiveRequest(SendRequest): name: str
class IRSignalCallbackData(BaseModel): format: str; freq: int; data: List[int]
class JobResponse(BaseModel): id: str; name: str; schedule_description: str; next_run: Optional[datetime]

# -----------------------------------------------------------------------------
# 5. FastAPIアプリケーション
# -----------------------------------------------------------------------------
app = FastAPI(title="Smart IR Remote Backend API", version="4.2.1")
templates = Jinja2Templates(directory="templates")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
@app.on_event("startup")
def on_startup(): init_db(); scheduler.start(); print("Scheduler started.")
@app.on_event("shutdown")
def on_shutdown(): scheduler.shutdown(); print("Scheduler shut down.")

# -----------------------------------------------------------------------------
# 6. ヘルパー関数
# -----------------------------------------------------------------------------
def format_job(job: Job) -> JobResponse:
    job_name = ""
    if 'execute_ir_send' in job.func_ref:
        job_name = f"単発: {job.args[0]}"
    elif 'execute_wakeup_alarm' in job.func_ref:
        job_name = f"目覚まし: {job.args[0]}/{job.args[1]}"
    
    trigger = job.trigger
    desc = "不明なスケジュール"
    if isinstance(trigger, DateTrigger):
        desc = f"一回のみ @ {trigger.run_date.strftime('%Y-%m-%d %H:%M')}"
    elif isinstance(trigger, CronTrigger):
        time_str = f"{str(trigger.fields[5]).zfill(2)}:{str(trigger.fields[6]).zfill(2)}"
        days_map = {"mon":"月", "tue":"火", "wed":"水", "thu":"木", "fri":"金", "sat":"土", "sun":"日"}
        if str(trigger.fields[3]) == "*" and str(trigger.fields[4]) == "*":
            desc = f"毎日 @ {time_str}"
        else:
            days = ",".join([days_map.get(d, d) for d in str(trigger.fields[4]).split(',')])
            desc = f"毎週 [{days}] @ {time_str}"
            
    return JobResponse(id=job.id, name=job_name, schedule_description=desc, next_run=job.next_run_time)

# -----------------------------------------------------------------------------
# 7. APIエンドポイント (DB接続を各関数内で完結)
# -----------------------------------------------------------------------------
@app.get("/", include_in_schema=False)
def read_root(request: Request):
    conn = sqlite3.connect(DATABASE_FILE); conn.row_factory = sqlite3.Row
    try:
        cursor = conn.cursor(); cursor.execute("SELECT id, name FROM ir_signals ORDER BY name"); signals = cursor.fetchall()
        return templates.TemplateResponse("index.html", {"request": request, "signals": [dict(row) for row in signals]})
    finally:
        conn.close()

@app.get("/api/signals", response_model=List[SignalListResponse])
def get_all_signals():
    conn = sqlite3.connect(DATABASE_FILE); conn.row_factory = sqlite3.Row
    try:
        cursor = conn.cursor(); cursor.execute("SELECT id, name FROM ir_signals"); signals = cursor.fetchall()
        return [dict(row) for row in signals]
    finally:
        conn.close()

@app.post("/api/signals", response_model=SignalResponse, status_code=201)
def create_signal(signal: SignalCreate):
    conn = sqlite3.connect(DATABASE_FILE)
    try:
        cursor = conn.cursor(); raw_data_json = json.dumps(signal.raw_data)
        cursor.execute("INSERT INTO ir_signals (name, raw_data) VALUES (?, ?)", (signal.name, raw_data_json)); conn.commit()
        return SignalResponse(id=cursor.lastrowid, name=signal.name, raw_data=signal.raw_data)
    except sqlite3.IntegrityError:
        raise HTTPException(409, f"Signal name '{signal.name}' already exists.")
    finally:
        conn.close()

@app.get("/api/signals/{name}", response_model=SignalResponse)
def get_signal_by_name(name: str):
    conn = sqlite3.connect(DATABASE_FILE); conn.row_factory = sqlite3.Row
    try:
        cursor = conn.cursor(); cursor.execute("SELECT * FROM ir_signals WHERE name = ?", (name,)); db_signal = cursor.fetchone()
        if not db_signal: raise HTTPException(404, f"Signal '{name}' not found.")
        return SignalResponse(id=db_signal["id"], name=db_signal["name"], raw_data=json.loads(db_signal["raw_data"]))
    finally:
        conn.close()

@app.put("/api/signals/{name}", response_model=SignalResponse)
def update_signal(name: str, signal_update: SignalUpdate):
    conn = sqlite3.connect(DATABASE_FILE)
    try:
        cursor = conn.cursor(); cursor.execute("SELECT id FROM ir_signals WHERE name = ?", (name,))
        if not cursor.fetchone(): raise HTTPException(404, f"Signal '{name}' not found.")
        
        updated_name = signal_update.name or name
        if signal_update.name and signal_update.name != name: cursor.execute("UPDATE ir_signals SET name = ? WHERE name = ?", (updated_name, name))
        if signal_update.raw_data: cursor.execute("UPDATE ir_signals SET raw_data = ? WHERE name = ?", (json.dumps(signal_update.raw_data), updated_name))
        conn.commit()
        # 更新後のデータを取得するために、新しい接続でget_signal_by_nameを内部的に呼び出す
        return get_signal_by_name(updated_name)
    finally:
        conn.close()

@app.delete("/api/signals/{name}")
def delete_signal(name: str):
    conn = sqlite3.connect(DATABASE_FILE)
    try:
        cursor = conn.cursor(); cursor.execute("DELETE FROM ir_signals WHERE name = ?", (name,)); conn.commit()
        if cursor.rowcount == 0: raise HTTPException(404, f"Signal '{name}' not found.")
        return {"message": f"Signal '{name}' deleted."}
    finally:
        conn.close()

@app.post("/api/callback/ir_signal/{name}", status_code=201, include_in_schema=False)
def receive_ir_signal_callback(name: str, data: IRSignalCallbackData):
    conn = sqlite3.connect(DATABASE_FILE)
    try:
        cursor = conn.cursor(); raw_data_json = json.dumps(data.data)
        cursor.execute("INSERT INTO ir_signals (name, raw_data) VALUES (?, ?) ON CONFLICT(name) DO UPDATE SET raw_data = excluded.raw_data;", (name, raw_data_json))
        conn.commit()
        return {"status": "ok"}
    finally:
        conn.close()

# --- 非DBアクセスAPI (変更なし) ---
@app.post("/api/send/{name}")
def send_signal_to_esp32(name: str, req_body: SendRequest):
    execute_ir_send(name, req_body.esp32_ip); return {"status": "ok"}

@app.post("/api/schedule", status_code=202)
def schedule_signal(req: ScheduleRequest):
    job_id, trigger = str(uuid.uuid4()), None
    if req.repeat_type == "once":
        if not req.execute_date: raise HTTPException(400, "Date is required")
        trigger = DateTrigger(run_date=datetime.combine(req.execute_date, req.execute_time))
    elif req.repeat_type in ["daily", "weekly"]:
        cron_args = {"hour": req.execute_time.hour, "minute": req.execute_time.minute}
        if req.repeat_type == "weekly":
            if not req.repeat_days: raise HTTPException(400, "Days are required")
            cron_args["day_of_week"] = ",".join(req.repeat_days)
        trigger = CronTrigger(**cron_args)
    if not trigger: raise HTTPException(400, "Invalid repeat_type")
    scheduler.add_job(execute_ir_send, trigger, args=[req.name, req.esp32_ip], id=job_id)
    return {"status": "ok", "message": f"Job '{req.name}' scheduled."}

@app.post("/api/schedule/wakeup", status_code=202)
def schedule_wakeup(req: WakeupScheduleRequest):
    job_id, trigger = str(uuid.uuid4()), None
    job_args = [req.on_signal_name, req.off_signal_name, req.interval_seconds, req.duration_seconds, req.esp32_ip]
    if req.repeat_type == "once":
        if not req.execute_date: raise HTTPException(400, "Date is required")
        trigger = DateTrigger(run_date=datetime.combine(req.execute_date, req.execute_time))
    elif req.repeat_type in ["daily", "weekly"]:
        cron_args = {"hour": req.execute_time.hour, "minute": req.execute_time.minute}
        if req.repeat_type == "weekly":
            if not req.repeat_days: raise HTTPException(400, "Days are required")
            cron_args["day_of_week"] = ",".join(req.repeat_days)
        trigger = CronTrigger(**cron_args)
    if not trigger: raise HTTPException(400, "Invalid repeat_type")
    scheduler.add_job(execute_wakeup_alarm, trigger, args=job_args, id=job_id)
    return {"status": "ok", "message": f"Wakeup alarm '{req.on_signal_name}' scheduled."}

@app.get("/api/schedules", response_model=List[JobResponse])
def get_scheduled_jobs():
    jobs = scheduler.get_jobs()
    # MODIFIED: Changed j.next_run_time to j.next_run
    formatted_jobs = [format_job(job) for job in jobs]
    return sorted(formatted_jobs, key=lambda j: j.next_run or datetime.max.replace(tzinfo=j.next_run.tzinfo if j.next_run else None))

@app.delete("/api/schedules/{job_id}", status_code=200)
def delete_scheduled_job(job_id: str):
    try: scheduler.remove_job(job_id)
    except JobLookupError: raise HTTPException(404, f"Job '{job_id}' not found.")
    return {"message": f"Job '{job_id}' deleted."}

@app.post("/api/receive", status_code=202)
def start_receive_mode(req: ReceiveRequest):
    callback_url = f"http://{SERVER_IP}:{SERVER_PORT}/api/callback/ir_signal/{req.name}"
    payload = {"mode": "receive", "timeout": 15000, "callback_url": callback_url}
    try:
        with httpx.Client() as c: c.put(f"http://{req.esp32_ip}/mode", json=payload, timeout=5.0).raise_for_status()
        return {"status": "ok"}
    except (httpx.RequestError, httpx.HTTPStatusError) as e:
        raise HTTPException(502, f"Failed to communicate with ESP32: {e}")

# -----------------------------------------------------------------------------
# 8. サーバー実行
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=SERVER_PORT)

