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

def build_atempo_filter(speed: float) -> str:
    # FFmpeg atempo filter only supports 0.5 to 2.0. Chain multiple filters if out of range.
    filters = []
    current = speed
    while current > 2.0:
        filters.append("atempo=2.0")
        current /= 2.0
    while current < 0.5:
        filters.append("atempo=0.5")
        current /= 0.5
    filters.append(f"atempo={current:.4f}")
    return ",".join(filters)

def check_has_audio(ffmpeg_exe: str, input_path: str) -> bool:
    try:
        proc = subprocess.run(
            [ffmpeg_exe, "-i", input_path],
            stderr=subprocess.PIPE,
            stdout=subprocess.PIPE,
            universal_newlines=True,
            timeout=5,
        )
        return "Audio:" in proc.stderr
    except Exception:
        return True

def process_video_task(task_id: str, input_path: str, speed: float):
    if task_id not in tasks:
        try:
            os.remove(input_path)
        except OSError:
            pass
        return

    output_path = tasks[task_id]["output_path"]
    video_speed = 1.0 / speed

    tasks[task_id]["status"] = "processing"
    stderr_lines = []
    try:
        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        has_audio = check_has_audio(ffmpeg_exe, input_path)

        if has_audio:
            atempo_filter = build_atempo_filter(speed)
            filter_complex = f'[0:v]setpts={video_speed}*PTS[v];[0:a]{atempo_filter}[a]'
            map_args = ['-map', '[v]', '-map', '[a]']
        else:
            filter_complex = f'[0:v]setpts={video_speed}*PTS[v]'
            map_args = ['-map', '[v]']

        cmd = [
            ffmpeg_exe, '-i', input_path,
            '-filter_complex', filter_complex,
            *map_args,
            '-c:v', 'libx264', '-preset', 'fast', '-crf', '28',
            '-movflags', '+faststart',
            output_path, '-y'
        ]
        
        process = subprocess.Popen(
            cmd,
            stderr=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            universal_newlines=True
        )
        
        duration_secs = 0.0
        
        for line in process.stderr:
            stderr_lines.append(line)
            if not duration_secs:
                dur_match = re.search(r"Duration:\s*(\d{2}:\d{2}:\d{2}\.\d{2})", line)
                if dur_match:
                    duration_secs = parse_time_to_seconds(dur_match.group(1))
            
            time_match = re.search(r"time=\s*(\d{2}:\d{2}:\d{2}\.\d{2})", line)
            if time_match and duration_secs > 0:
                current_secs = parse_time_to_seconds(time_match.group(1))
                target_duration = duration_secs * video_speed
                progress = (current_secs / target_duration) * 100
                if task_id in tasks:
                    tasks[task_id]["progress"] = min(99, int(progress))
                
        process.wait()
        
        if task_id in tasks:
            if process.returncode == 0:
                tasks[task_id]["status"] = "completed"
                tasks[task_id]["progress"] = 100
                tasks[task_id]["outputUrl"] = f"/api/download/{task_id}"
                tasks[task_id]["output_url"] = f"/api/download/{task_id}"
            else:
                err_detail = "".join(stderr_lines[-10:])
                print(f"[ERROR] Task {task_id} FFmpeg failed (code {process.returncode}):\n{err_detail}")
                tasks[task_id]["status"] = "error"
                tasks[task_id]["progress"] = 0
                tasks[task_id]["error"] = f"FFmpeg processing failed (code {process.returncode})"
                try:
                    os.remove(output_path)
                except OSError:
                    pass
    except Exception as exc:
        print(f"[ERROR] Task {task_id} exception: {exc}")
        if task_id in tasks:
            tasks[task_id]["status"] = "error"
            tasks[task_id]["progress"] = 0
            tasks[task_id]["error"] = str(exc)
        try:
            os.remove(output_path)
        except OSError:
            pass
    finally:
        try:
            os.remove(input_path)
        except OSError:
            pass
        if task_id in tasks:
            tasks[task_id].pop("input_path", None)

@app.post("/api/process")
async def process_video(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(None),
    video: UploadFile = File(None),
    speed: float = Form(1.5),
):
    cleanup_stale_tasks()
    upload = file or video
    if not upload:
        return JSONResponse(status_code=400, content={"error": "No video file uploaded"})

    task_id = str(uuid.uuid4())
    filename = upload.filename or "input.mp4"
    input_path = os.path.join(TEMP_DIR, f"{task_id}_in_{filename}")
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
            while chunk := await upload.read(1024 * 1024):  # 1MB chunks
                buffer.write(chunk)
    except Exception as exc:
        print(f"[ERROR] Failed to save upload for {task_id}: {exc}")
        tasks.pop(task_id, None)
        try:
            os.remove(input_path)
        except OSError:
            pass
        return JSONResponse(status_code=500, content={"error": "Failed to save uploaded file"})
        
    background_tasks.add_task(process_video_task, task_id, input_path, speed)
    return {"taskId": task_id, "task_id": task_id}

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

    return FileResponse(
        output_path,
        filename="processed_video.mp4",
        media_type="video/mp4",
    )
