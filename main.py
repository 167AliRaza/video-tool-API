import os
import uuid
import subprocess
import re
import tempfile
import asyncio
import imageio_ffmpeg
from fastapi import FastAPI, UploadFile, File, Form, BackgroundTasks
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Adjust this to the specific frontend URL in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

tasks = {}
TEMP_DIR = tempfile.gettempdir()

def parse_time_to_seconds(time_str: str) -> float:
    # Parses HH:MM:SS.ms into total seconds
    parts = time_str.split(':')
    if len(parts) == 3:
        return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
    return 0.0

def process_video_task(task_id: str, input_path: str, speed: float):
    output_path = os.path.join(TEMP_DIR, f"{task_id}_out.mp4")
    
    video_speed = 1.0 / speed
    audio_speed = speed

    tasks[task_id]["status"] = "processing"
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
            tasks[task_id]["progress"] = min(99, int(progress))
            
    process.wait()
    
    if process.returncode == 0:
        tasks[task_id]["status"] = "completed"
        tasks[task_id]["progress"] = 100
        tasks[task_id]["output_path"] = output_path
    else:
        tasks[task_id]["status"] = "error"
        tasks[task_id]["progress"] = 0

    # Cleanup the input file
    try:
        os.remove(input_path)
    except:
        pass

@app.post("/api/process")
async def process_video(background_tasks: BackgroundTasks, file: UploadFile = File(...), speed: float = Form(...)):
    task_id = str(uuid.uuid4())
    
    # Save uploaded file
    input_path = os.path.join(TEMP_DIR, f"{task_id}_in_{file.filename}")
    with open(input_path, "wb") as buffer:
        content = await file.read()
        buffer.write(content)
        
    tasks[task_id] = {
        "status": "starting",
        "progress": 0,
    }
    
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
    return FileResponse(tasks[task_id]["output_path"], filename="processed_video.mp4", media_type="video/mp4")
