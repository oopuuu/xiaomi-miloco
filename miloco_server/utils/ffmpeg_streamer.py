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
from typing import Optional
from miloco_server.config import RTSP_PORT

logger = logging.getLogger(__name__)

# [新增] 增强型内存监控
def get_process_status():
    try:
        process = psutil.Process(os.getpid())
        mem_mb = process.memory_info().rss / 1024 / 1024
        # 检查打开的文件描述符数量，防止句柄泄露
        fds = process.num_fds() if hasattr(process, 'num_fds') else 0
        return f"Mem: {mem_mb:.1f}MB | FDs: {fds}"
    except:
        return "Status: N/A"

class PipeWriter:
    def __init__(self, pipe_path, name, is_video=False):
        self.pipe_path = pipe_path
        self.name = name
        self.is_video = is_video
        self.fd = None
        self._broken = False
        self._write_count = 0
        self._drop_count = 0
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
                # 视频 1MB, 音频 64KB (折中方案)
                size = 1048576 if self.is_video else 65536
                fcntl.fcntl(self.fd, F_SETPIPE_SZ, size)
                logger.info(f"[{self.name}] Pipe buffer set to {size/1024:.0f}KB")
            except Exception:
                logger.warning(f"[{self.name}] Failed to set pipe buffer size")
                pass
            
            logger.info(f"[{self.name}] Pipe opened: {self.pipe_path}")
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
            self._write_count += 1
            # [日志] 每写入 1000 次数据包，打印一次状态，确认管道通畅
            if self._write_count % 1000 == 0:
                logger.debug(f"[{self.name}] Stats: Written={self._write_count}, Dropped={self._drop_count}")
        except BlockingIOError:
            # [日志] 管道阻塞（满）时，打印警告。如果频繁出现，说明 FFmpeg 消费太慢
            self._drop_count += 1
            if self._drop_count % 100 == 0:
                logger.warning(f"[{self.name}] Pipe FULL! Dropped {self._drop_count} packets. FFmpeg is too slow?")
            pass 
        except (BrokenPipeError, OSError) as e:
            if not self._broken:
                logger.error(f"[{self.name}] Pipe broken: {e}")
                self._broken = True
            self.close()

    def close(self):
        if self.fd:
            try:
                os.close(self.fd)
                logger.info(f"[{self.name}] Pipe closed.")
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
    _start_lock = threading.Lock()
    _global_cooldown_until = 0
    _global_cooldown_step = 10 

    def __init__(self, camera_id: str, rtsp_target=None):
        self.camera_id = camera_id
        self.rtsp_url = rtsp_target or f"rtsp://127.0.0.1:{RTSP_PORT}/{camera_id}"
        self.pipe_video = f"/tmp/miloco_video_{camera_id}.pipe"
        self.pipe_audio = f"/tmp/miloco_audio_{camera_id}.pipe"
        self.video_writer: Optional[PipeWriter] = None
        self.audio_writer: Optional[PipeWriter] = None
        self.process: Optional[subprocess.Popen] = None
        self._last_health_check = 0
        self._stop_event = threading.Event()

    def _force_kill_zombies(self):
        try:
            cmd = f"pgrep -f 'ffmpeg.*{self.camera_id}'"
            pids = subprocess.check_output(cmd, shell=True).decode().strip().split('\n')
            count = 0
            for pid in pids:
                if pid and pid.isdigit() and int(pid) != os.getpid():
                    os.kill(int(pid), 9) 
                    count += 1
            if count > 0:
                logger.warning(f"Force killed {count} zombie FFmpeg processes.")
        except:
            pass

    def start(self, video_codec="hevc"):
        if time.time() < FFmpegStreamer._global_cooldown_until:
            logger.info(f"Start ignored (Cooling down until {FFmpegStreamer._global_cooldown_until})")
            return
            
        if not self._start_lock.acquire(blocking=False):
            logger.info("Start ignored (Locked)")
            return

        try:
            self.stop() 
            self._force_kill_zombies()
            time.sleep(0.5)

            self._stop_event.clear()
            self._last_health_check = time.time()

            hw_accel = os.getenv("MILOCO_HW_ACCEL", "cpu").lower()
            hw_device = os.getenv("MILOCO_HW_DEVICE", "/dev/dri/renderD128")

            global_args = []
            video_filters = ["setpts=PTS-STARTPTS"]
            video_out_args = []
            common_opts = ['-bf', '0']

            if hw_accel in ["intel", "amd", "vaapi"]:
                logger.info(f"FFmpeg Mode: Hybrid VAAPI ({self.camera_id})")
                global_args = [
                    '-init_hw_device', f'vaapi=va:{hw_device}',
                    '-filter_hw_device', 'va'
                ]
                video_filters.extend(['format=nv12', 'hwupload'])
                # 使用 CQP (q=28) 兼顾画质与码率稳定
                video_out_args = [
                    '-c:v', 'h264_vaapi',
                    '-async_depth', '1',
                    '-rc_mode', 'CQP',
                    '-global_quality', '28',
                    '-profile:v', 'main'
                ] + common_opts
                
            elif hw_accel in ["nvidia", "nvenc", "cuda"]:
                logger.info(f"FFmpeg Mode: NVIDIA ({self.camera_id})")
                video_out_args = ['-c:v', 'h264_nvenc', '-preset', 'p1', '-tune', 'zerolatency'] + common_opts
            else:
                logger.info(f"FFmpeg Mode: CPU ({self.camera_id})")
                video_out_args = ['-c:v', 'libx264', '-preset', 'ultrafast', '-tune', 'zerolatency'] + common_opts

            video_filter_str = ",".join(video_filters)
            audio_filter_chain = "aresample=async=1:min_hard_comp=0.100000:first_pts=0"

            try:
                self.video_writer = PipeWriter(self.pipe_video, "Video", is_video=True)
                self.audio_writer = PipeWriter(self.pipe_audio, "Audio", is_video=False)

                ffmpeg_cmd = [
                    'ffmpeg', '-y',
                    '-hide_banner',
                    '-loglevel', 'warning', # 保持 warning 避免日志爆炸，关键错误 monitor 会抓
                    '-stats',

                    '-fflags', '+genpts+nobuffer+igndts',
                    '-flags', 'low_delay',
                    '-reinit_filter', '0',
                    
                    '-analyzeduration', '1000000', 
                    '-probesize', '1000000',       
                    '-err_detect', 'ignore_err',
                ]

                ffmpeg_cmd.extend(global_args)
                ffmpeg_cmd.extend([
                    '-thread_queue_size', '512',
                    '-f', video_codec,
                    '-use_wallclock_as_timestamps', '1', # 视频用墙钟
                    '-i', self.pipe_video,

                    '-thread_queue_size', '512',
                    '-f', 's16le', '-ar', '16000', '-ac', '1',
                    '-i', self.pipe_audio, # 音频用流时间戳

                    '-map', '0:v', '-map', '1:a',
                    '-vf', video_filter_str,
                    '-af', audio_filter_chain,

                    *video_out_args,
                    
                    '-bsf:v', 'h264_mp4toannexb',

                    '-c:a', 'aac', '-ar', '16000', '-b:a', '64k',
                    '-max_interleave_delta', '0',
                    '-vsync', '0',
                    
                    '-f', 'rtsp', '-rtsp_transport', 'tcp', '-max_muxing_queue_size', '1024',
                    self.rtsp_url,
                ])

                logger.info(f"Launching FFmpeg: {' '.join(ffmpeg_cmd)}")
                
                self.process = subprocess.Popen(
                    ffmpeg_cmd, 
                    stdout=subprocess.DEVNULL, 
                    stderr=subprocess.PIPE,
                    text=True,  
                    bufsize=1   
                )
                threading.Thread(target=self._monitor_ffmpeg, daemon=True).start()

            except Exception as e:
                logger.error(f"FFmpeg start failed exception: {e}")
                self.stop()
        finally:
            self._start_lock.release()

    def _trigger_restart(self, reason):
        logger.error(f"[Watchdog] {reason}. Triggering cooldown.")
        FFmpegStreamer._global_cooldown_until = time.time() + FFmpegStreamer._global_cooldown_step
        FFmpegStreamer._global_cooldown_step = min(FFmpegStreamer._global_cooldown_step + 10, 60)
        self.stop()

    def _monitor_ffmpeg(self):
        if not self.process: return
        
        logger.info(f"[Monitor] FFmpeg process started (PID: {self.process.pid})")
        fd = self.process.stderr.fileno()
        os.set_blocking(fd, False)
        
        pattern = re.compile(r'frame=\s*(\d+).*fps=\s*([\d.]+).*speed=\s*([\d.]+)x')
        ignore_keywords = ['pps', 'nalu', 'header', 'buffer', 'frame rate', 'unspecified', 'params', 'last message repeated', 'deprecated']

        last_log_time = time.time()

        while not self._stop_event.is_set():
            if self.process.poll() is not None:
                # [日志] 进程意外退出
                if self.process.returncode not in [0, -9, 234, 111]:
                    logger.error(f"[Monitor] FFmpeg exited unexpectedly (Code: {self.process.returncode})")
                else:
                    logger.info(f"[Monitor] FFmpeg stopped normally (Code: {self.process.returncode})")
                break

            try:
                ready = select.select([fd], [], [], 1.0)
                if ready[0]:
                    line = self.process.stderr.readline()
                    if not line: continue
                    l = line.strip()
                    if not l: continue

                    if "frame=" in l:
                        now = time.time()
                        # [日志] 每 60 秒打印一次存活状态和内存占用
                        if (now - self._last_health_check > 60): 
                            match = pattern.search(l)
                            if match:
                                stats = get_process_status()
                                logger.info(f"[Monitor] Alive | {match.group(0)} | {stats}")
                                self._last_health_check = now
                                FFmpegStreamer._global_cooldown_step = 10
                    else:
                        is_fatal = any(x in l.lower() for x in ['failed', 'unable', 'no such', 'fatal', 'error'])
                        is_ignored = any(k in l.lower() for k in ignore_keywords)
                        
                        # [日志] 捕获并打印非 frame 信息的关键错误
                        if is_fatal and not is_ignored:
                            if "error opening output file" in l.lower():
                                logger.warning(f"[Monitor] Port busy or connection rejected: {l}")
                                break 
                            
                            logger.error(f"[FFmpeg Error] {l}")
                            
                            if "invalid argument" in l.lower():
                                self._trigger_restart(f"Fatal Config: {l}")
                                return
            except Exception as e:
                logger.error(f"[Monitor] Exception loop: {e}")
                break
        
        self.stop()

    def stop(self):
        self._stop_event.set()
        
        if self.video_writer: 
            self.video_writer.close(); self.video_writer = None
        if self.audio_writer: 
            self.audio_writer.close(); self.audio_writer = None
            
        if self.process:
            logger.info(f"Stopping FFmpeg process (PID: {self.process.pid})...")
            try:
                self.process.terminate()
                self.process.wait(timeout=2)
            except:
                try:
                    logger.warning("Terminating timed out, killing process...")
                    self.process.kill()
                except:
                    pass
            self.process = None
            
        gc.collect()

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
