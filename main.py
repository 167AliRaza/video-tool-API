import os
import uuid
import subprocess
import re
import tempfile
import asyncio
import time
from contextlib import asynccontextmanager
import imageio_ffmpeg
from fastapi import FastAPI, UploadFile, File, Form, BackgroundTasks
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.background import BackgroundTask

tasks = {}
TEMP_DIR = tempfile.gettempdir()
TASK_TTL_SECONDS = 600  # 10 minutes

def cleanup_stale_tasks():
    now = time.time()
    stale_ids = [tid for tid, t in tasks.items() if now - t.get("created_at", 0) > TASK_TTL_SECONDS]
    for tid in stale_ids:
        task_data = tasks.pop(tid, {})
        for path_key in ("input_path", "output_path"):
            path = task_data.get(path_key)
            if path:
                try:
                    os.remove(path)
                except OSError:
                    pass

@asynccontextmanager
async def lifespan(app: FastAPI):
    async def periodic_cleanup():
        while True:
            await asyncio.sleep(60)  # check every minute
            cleanup_stale_tasks()
    timer_task = asyncio.create_task(periodic_cleanup())
    yield
    timer_task.cancel()

app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Adjust this to the specific frontend URL in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def parse_time_to_seconds(time_str: str) -> float:
    # Parses HH:MM:SS.ms into total seconds
    parts = time_str.split(':')
    if len(parts) == 3:
        return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
    return 0.0

def process_video_task(task_id: str, input_path: str, speed: float):
    if task_id not in tasks:
        # Task was cleaned up before processing started
        try:
            os.remove(input_path)
        except OSError:
            pass
        return

    output_path = tasks[task_id]["output_path"]
    video_speed = 1.0 / speed
    audio_speed = speed

    tasks[task_id]["status"] = "processing"
    try:
        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        cmd = [
            ffmpeg_exe, '-i', input_path,
            '-filter_complex', f'[0:v]setpts={video_speed}*PTS[v];[0:a]atempo={audio_speed}[a]',
            '-map', '[v]', '-map', '[a]',
            '-c:v', 'libx264', '-preset', 'fast', '-crf', '28',
            '-movflags', '+faststart',
            output_path, '-y'
        ]
        
        # Run FFmpeg and capture stderr (where progress info is printed)
        process = subprocess.Popen(cmd, stderr=subprocess.PIPE, stdout=subprocess.PIPE, universal_newlines=True)
        
        duration_secs = 0.0
        
        for line in process.stderr:
            if not duration_secs:
                dur_match = re.search(r"Duration:\s*(\d{2}:\d{2}:\d{2}\.\d{2})", line)
                if dur_match:
                    duration_secs = parse_time_to_seconds(dur_match.group(1))
            
            time_match = re.search(r"time=\s*(\d{2}:\d{2}:\d{2}\.\d{2})", line)
            if time_match and duration_secs > 0:
                current_secs = parse_time_to_seconds(time_match.group(1))
                # The target duration of the output video is Original Duration * VideoSpeed Modifier
                target_duration = duration_secs * video_speed
                progress = (current_secs / target_duration) * 100
                if task_id in tasks:
                    tasks[task_id]["progress"] = min(99, int(progress))
                
        process.wait()
        
        if task_id in tasks:
            if process.returncode == 0:
                tasks[task_id]["status"] = "completed"
                tasks[task_id]["progress"] = 100
            else:
                tasks[task_id]["status"] = "error"
                tasks[task_id]["progress"] = 0
                try:
                    os.remove(output_path)
                except OSError:
                    pass
    except Exception:
        if task_id in tasks:
            tasks[task_id]["status"] = "error"
            tasks[task_id]["progress"] = 0
        try:
            os.remove(output_path)
        except OSError:
            pass
    finally:
        # Guarantee cleanup of input file even on failure or cancellation
        try:
            os.remove(input_path)
        except OSError:
            pass
        if task_id in tasks:
            tasks[task_id].pop("input_path", None)

@app.post("/api/process")
async def process_video(background_tasks: BackgroundTasks, file: UploadFile = File(...), speed: float = Form(...)):
    cleanup_stale_tasks()
    task_id = str(uuid.uuid4())
    
    # Save uploaded file in chunks to avoid loading entire video into memory
    input_path = os.path.join(TEMP_DIR, f"{task_id}_in_{file.filename}")
    output_path = os.path.join(TEMP_DIR, f"{task_id}_out.mp4")
    
    tasks[task_id] = {
        "status": "starting",
        "progress": 0,
        "created_at": time.time(),
        "input_path": input_path,
        "output_path": output_path,
    }

    try:
        with open(input_path, "wb") as buffer:
            while chunk := await file.read(1024 * 1024):  # 1MB chunks
                buffer.write(chunk)
    except Exception:
        tasks.pop(task_id, None)
        try:
            os.remove(input_path)
        except OSError:
            pass
        return JSONResponse(status_code=500, content={"error": "Failed to save uploaded file"})
        
    background_tasks.add_task(process_video_task, task_id, input_path, speed)
    return {"task_id": task_id}

@app.get("/api/status/{task_id}")
async def get_status(task_id: str):
    if task_id not in tasks:
        return JSONResponse(status_code=404, content={"error": "Task not found"})
    return tasks[task_id]

@app.get("/api/download/{task_id}")
async def download_video(task_id: str):
    if task_id not in tasks or tasks[task_id]["status"] != "completed":
        return JSONResponse(status_code=400, content={"error": "File not ready"})
    output_path = tasks[task_id]["output_path"]

    def cleanup_after_download():
        try:
            os.remove(output_path)
        except OSError:
            pass
        tasks.pop(task_id, None)

    return FileResponse(
        output_path,
        filename="processed_video.mp4",
        media_type="video/mp4",
        background=BackgroundTask(cleanup_after_download),
    )
