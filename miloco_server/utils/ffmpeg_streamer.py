import os
import logging
import subprocess
import threading
import time
import re
import fcntl
import gc
import psutil
import select
from typing import Optional, Dict
from miloco_server.config import RTSP_PORT

logger = logging.getLogger(__name__)

def get_memory_usage():
    try:
        process = psutil.Process(os.getpid())
        mem = process.memory_info().rss / 1024 / 1024
        return f"{mem:.1f} MB"
    except:
        return "N/A"

class PipeWriter:
    def __init__(self, pipe_path, name, is_video=False):
        self.pipe_path = pipe_path
        self.name = name
        self.is_video = is_video
        self.fd = None
        self._broken = False
        self._ensure_pipe()

    def _ensure_pipe(self):
        self.close()
        try:
            if os.path.exists(self.pipe_path):
                os.remove(self.pipe_path)
            os.mkfifo(self.pipe_path)
            
            # 使用非阻塞模式
            self.fd = os.open(self.pipe_path, os.O_RDWR | os.O_NONBLOCK)
            try:
                F_SETPIPE_SZ = 1031
                size = 1048576 if self.is_video else 16384
                fcntl.fcntl(self.fd, F_SETPIPE_SZ, size)
            except Exception:
                pass
            logger.debug(f"[{self.name}] Pipe opened: {self.pipe_path}")
            self._broken = False
            return True
        except Exception as e:
            logger.error(f"[{self.name}] Pipe creation error: {e}")
            self._broken = True
            return False

    def write_direct(self, data: bytes):
        if self.fd is None: return
        try:
            os.write(self.fd, data)
        except BlockingIOError:
            pass 
        except (BrokenPipeError, OSError) as e:
            if not self._broken:
                logger.warning(f"[{self.name}] Pipe broken: {e}")
                self._broken = True
            self.close()

    def close(self):
        if self.fd:
            try:
                os.close(self.fd)
            except:
                pass
            self.fd = None
        if os.path.exists(self.pipe_path):
            try:
                os.remove(self.pipe_path)
            except:
                pass

    def __del__(self):
        self.close()


class FFmpegStreamer:
    _active_processes: Dict[str, subprocess.Popen] = {}
    _process_lock = threading.Lock()
    _global_cooldown_until = 0
    _global_cooldown_step = 10 

    def __init__(self, camera_id: str, rtsp_target=None):
        self.camera_id = camera_id
        self.rtsp_url = rtsp_target or f"rtsp://127.0.0.1:{RTSP_PORT}/{camera_id}"
        
        self.pipe_video = f"/tmp/miloco_video_{camera_id}.pipe"
        self.pipe_audio = f"/tmp/miloco_audio_{camera_id}.pipe"
        self.video_writer: Optional[PipeWriter] = None
        self.audio_writer: Optional[PipeWriter] = None
        self._last_health_check = 0
        self._stop_event = threading.Event()

    def _force_kill_zombies(self):
        try:
            cmd = f"pgrep -f 'ffmpeg.*{self.camera_id}'"
            pids = subprocess.check_output(cmd, shell=True).decode().strip().split('\n')
            for pid in pids:
                if pid and pid.isdigit() and int(pid) != os.getpid():
                    logger.warning(f"Killing zombie FFmpeg process: {pid}")
                    os.kill(int(pid), 9) 
        except:
            pass

    def start(self, video_codec="hevc"):
        if time.time() < FFmpegStreamer._global_cooldown_until:
            return

        with FFmpegStreamer._process_lock:
            self._kill_active_process()
            self._force_kill_zombies()
            time.sleep(1.0) 

            self._stop_event.clear()
            self._last_health_check = time.time()

            hw_accel = os.getenv("MILOCO_HW_ACCEL", "cpu").lower()
            hw_device = os.getenv("MILOCO_HW_DEVICE", "/dev/dri/renderD128")

            global_args = []
            video_filters = ["setpts=PTS-STARTPTS"]
            video_out_args = []
            
            # [极简模式] 只设置最基本的转码参数，移除所有可能冲突的高级参数
            if hw_accel in ["intel", "amd", "vaapi"]:
                logger.info(f"FFmpeg Mode: Hybrid VAAPI ({self.camera_id})")
                global_args = [
                    '-init_hw_device', f'vaapi=va:{hw_device}',
                    '-filter_hw_device', 'va'
                ]
                video_filters.extend(['format=nv12', 'hwupload'])
                video_out_args = [
                    '-c:v', 'h264_vaapi',
                    '-qp', '25',          # 最通用的质量参数
                    '-profile:v', 'main'
                ]

            elif hw_accel in ["nvidia", "nvenc", "cuda"]:
                logger.info(f"FFmpeg Mode: NVIDIA ({self.camera_id})")
                video_out_args = [
                    '-c:v', 'h264_nvenc', 
                    '-preset', 'p1', 
                    '-tune', 'zerolatency'
                ]
            else:
                logger.info(f"FFmpeg Mode: CPU ({self.camera_id})")
                video_out_args = [
                    '-c:v', 'libx264', 
                    '-preset', 'ultrafast', 
                    '-tune', 'zerolatency'
                ]

            video_filter_str = ",".join(video_filters)
            # 音频同步参数保持不变，因为这是解决断音的关键
            audio_filter_chain = "aresample=async=1:min_hard_comp=0.100000:first_pts=0"

            try:
                self.video_writer = PipeWriter(self.pipe_video, "Video", is_video=True)
                self.audio_writer = PipeWriter(self.pipe_audio, "Audio", is_video=False)

                ffmpeg_cmd = [
                    'ffmpeg', '-y',
                    '-hide_banner',
                    '-loglevel', 'warning', 
                    '-stats',

                    '-fflags', '+genpts+nobuffer+igndts',
                    '-flags', 'low_delay',
                    
                    # [极简模式] 移除所有手动指定的 BSF，防止与编码器冲突
                    # 也不加 -reinit_filter 0
                    
                    '-analyzeduration', '1000000', 
                    '-probesize', '1000000',        
                    '-err_detect', 'ignore_err',
                ]

                ffmpeg_cmd.extend(global_args)
                ffmpeg_cmd.extend([
                    '-thread_queue_size', '128',
                    '-f', video_codec,
                    '-use_wallclock_as_timestamps', '1',
                    '-i', self.pipe_video,

                    '-thread_queue_size', '512',
                    '-f', 's16le', '-ar', '16000', '-ac', '1',
                    '-i', self.pipe_audio,

                    '-map', '0:v', '-map', '1:a',
                    '-vf', video_filter_str,
                    '-af', audio_filter_chain,

                    *video_out_args,
                    '-c:a', 'aac', '-ar', '16000', '-b:a', '64k',
                    '-max_interleave_delta', '0',
                    
                    # RTSP 输出配置
                    '-f', 'rtsp', 
                    '-rtsp_transport', 'tcp', 
                    '-max_muxing_queue_size', '400',
                    self.rtsp_url,
                ])

                process = subprocess.Popen(
                    ffmpeg_cmd, 
                    stdout=subprocess.DEVNULL, 
                    stderr=subprocess.PIPE,
                    text=True,  
                    bufsize=1   
                )
                
                FFmpegStreamer._active_processes[self.camera_id] = process
                threading.Thread(target=self._monitor_ffmpeg, args=(process,), daemon=True).start()

            except Exception as e:
                logger.error(f"FFmpeg start failed: {e}")
                self.stop()

    def _kill_active_process(self):
        if self.camera_id in FFmpegStreamer._active_processes:
            old_proc = FFmpegStreamer._active_processes[self.camera_id]
            try:
                old_proc.terminate()
                old_proc.wait(timeout=2)
            except:
                try:
                    old_proc.kill()
                except:
                    pass
            del FFmpegStreamer._active_processes[self.camera_id]
            gc.collect()

    def _trigger_restart(self, reason):
        logger.error(f"[Watchdog] {reason}. Cooling down.")
        FFmpegStreamer._global_cooldown_until = time.time() + FFmpegStreamer._global_cooldown_step
        FFmpegStreamer._global_cooldown_step = min(FFmpegStreamer._global_cooldown_step + 10, 60)
        self.stop()

    def _monitor_ffmpeg(self, proc):
        fd = proc.stderr.fileno()
        os.set_blocking(fd, False)
        
        pattern = re.compile(r'frame=\s*(\d+).*fps=\s*([\d.]+).*speed=\s*([\d.]+)x')
        ignore_keywords = ['pps', 'nalu', 'header', 'buffer', 'frame rate', 'unspecified', 'params', 'last message repeated', 'deprecated']

        while not self._stop_event.is_set():
            if proc.poll() is not None:
                if FFmpegStreamer._active_processes.get(self.camera_id) == proc:
                    logger.error(f"FFmpeg exited (Code: {proc.returncode})")
                    if proc.returncode in [234, 1, 111]:
                        self._force_kill_zombies()
                break

            try:
                ready = select.select([fd], [], [], 1.0)
                if ready[0]:
                    line = proc.stderr.readline()
                    if not line: continue
                    l = line.strip()
                    if not l: continue

                    if "frame=" in l:
                        now = time.time()
                        if (now - self._last_health_check > 60): 
                            match = pattern.search(l)
                            if match:
                                mem = get_memory_usage()
                                logger.info(f"[RTSP] Alive | {match.group(0)} | Mem: {mem}")
                                self._last_health_check = now
                                FFmpegStreamer._global_cooldown_step = 10
                    else:
                        is_fatal = any(x in l.lower() for x in ['failed', 'unable', 'no such', 'fatal', 'error'])
                        is_ignored = any(k in l.lower() for k in ignore_keywords)
                        if is_fatal and not is_ignored:
                            logger.error(f"[FFmpeg Error] {l}")
                            # 配置错误触发冷却，其他错误（如网络）不触发冷却以便重试
                            if "invalid argument" in l.lower() or "option not found" in l.lower():
                                self._trigger_restart(f"Fatal Config: {l}")
                                return
            except Exception:
                break
        
        with FFmpegStreamer._process_lock:
            if FFmpegStreamer._active_processes.get(self.camera_id) == proc:
                self.stop()

    def stop(self):
        self._stop_event.set()
        
        if self.video_writer: 
            self.video_writer.close(); self.video_writer = None
        if self.audio_writer: 
            self.audio_writer.close(); self.audio_writer = None
            
        with FFmpegStreamer._process_lock:
            self._kill_active_process()
            self._force_kill_zombies()

    def push_audio_raw(self, data: bytes):
        if self.audio_writer: self.audio_writer.write_direct(data)

    def push_video(self, data: bytes, seq: int, is_i_frame: bool = False):
        if self.video_writer:
            self.video_writer.write_direct(data)

    def __del__(self):
        try:
            self.stop()
        except:
            pass
