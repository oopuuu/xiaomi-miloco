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
            
            self.fd = os.open(self.pipe_path, os.O_RDWR | os.O_NONBLOCK)
            try:
                F_SETPIPE_SZ = 1031
                # [低延迟] 极小缓存，迫使数据实时流动
                # 视频 256KB (约1-2关键帧)，音频 16KB (约0.5s)
                size = 262144 if self.is_video else 16384
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
    # [关键修复] 全局进程注册表
    # 无论实例化多少个对象，都通过这个字典管理进程，确保能杀死旧进程
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
        
        # 注意：不再使用 self.process 来管理生命周期，而是用 _active_processes
        self._last_health_check = 0
        self._stop_event = threading.Event()

    def start(self, video_codec="hevc"):
        # 冷却检查
        if time.time() < FFmpegStreamer._global_cooldown_until:
            return

        with FFmpegStreamer._process_lock:
            # 1. 启动前，强制杀死该 camera_id 下已存在的任何进程
            self._kill_active_process()

            self._stop_event.clear()
            self._last_health_check = time.time()

            hw_accel = os.getenv("MILOCO_HW_ACCEL", "cpu").lower()
            hw_device = os.getenv("MILOCO_HW_DEVICE", "/dev/dri/renderD128")

            global_args = []
            video_filters = ["setpts=PTS-STARTPTS"]
            video_out_args = []
            common_opts = ['-g', '50', '-bf', '0']

            if hw_accel in ["intel", "amd", "vaapi"]:
                logger.info(f"FFmpeg Mode: Hybrid VAAPI ({self.camera_id})")
                global_args = [
                    '-init_hw_device', f'vaapi=va:{hw_device}',
                    '-filter_hw_device', 'va'
                ]
                video_filters.extend(['format=nv12', 'hwupload'])
                video_out_args = [
                    '-c:v', 'h264_vaapi',
                    '-async_depth', '1',
                    '-rc_mode', 'CQP',
                    '-global_quality', '25',
                    '-profile:v', 'main',
                    '-extra_hw_frames', '64'
                ] + common_opts
            elif hw_accel in ["nvidia", "nvenc", "cuda"]:
                logger.info(f"FFmpeg Mode: NVIDIA ({self.camera_id})")
                video_out_args = ['-c:v', 'h264_nvenc', '-preset', 'p1', '-tune', 'zerolatency'] + common_opts
            else:
                logger.info(f"FFmpeg Mode: CPU ({self.camera_id})")
                video_out_args = ['-c:v', 'libx264', '-preset', 'ultrafast', '-tune', 'zerolatency', '-profile:v', 'baseline'] + common_opts

            video_filter_str = ",".join(video_filters)
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
                    
                    '-bsf:v', 'hevc_mp4toannexb', 
                    
                    # [低延迟优化] 极小的探测缓冲，FFmpeg 只要读到一点点头信息就开始推流
                    '-analyzeduration', '100000', # 0.1秒
                    '-probesize', '32768',        # 32KB
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
                    '-f', 'rtsp', '-rtsp_transport', 'tcp', '-max_muxing_queue_size', '400',
                    self.rtsp_url,
                ])

                process = subprocess.Popen(
                    ffmpeg_cmd, 
                    stdout=subprocess.DEVNULL, 
                    stderr=subprocess.PIPE,
                    text=True,  
                    bufsize=1   
                )
                
                # 注册到全局字典
                FFmpegStreamer._active_processes[self.camera_id] = process
                
                threading.Thread(target=self._monitor_ffmpeg, args=(process,), daemon=True).start()

            except Exception as e:
                logger.error(f"FFmpeg start failed: {e}")
                self.stop()

    def _kill_active_process(self):
        """查找并杀死当前摄像头ID对应的所有旧进程"""
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
            # 强制回收
            gc.collect()
            # 增加一点点等待，确保端口释放
            time.sleep(0.5)

    def _trigger_restart(self, reason):
        logger.error(f"[Watchdog] {reason}. Cooling down.")
        FFmpegStreamer._global_cooldown_until = time.time() + FFmpegStreamer._global_cooldown_step
        FFmpegStreamer._global_cooldown_step = min(FFmpegStreamer._global_cooldown_step + 10, 60)
        self.stop()

    def _monitor_ffmpeg(self, proc):
        # 传递具体的 proc 对象，避免引用混淆
        fd = proc.stderr.fileno()
        os.set_blocking(fd, False)
        
        pattern = re.compile(r'frame=\s*(\d+).*fps=\s*([\d.]+).*speed=\s*([\d.]+)x')
        ignore_keywords = ['pps', 'nalu', 'header', 'buffer', 'frame rate', 'unspecified', 'params', 'last message repeated', 'deprecated']

        while not self._stop_event.is_set():
            if proc.poll() is not None:
                # 只有当它是当前活跃进程时才报错，避免旧进程退出的干扰
                if FFmpegStreamer._active_processes.get(self.camera_id) == proc:
                    logger.error(f"FFmpeg exited (Code: {proc.returncode})")
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
                            if "invalid argument" in l.lower() or "function not implemented" in l.lower():
                                self._trigger_restart(f"Fatal Config Error: {l}")
                                return
            except Exception:
                break
        
        # 监控结束，如果是当前进程，则清理
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
