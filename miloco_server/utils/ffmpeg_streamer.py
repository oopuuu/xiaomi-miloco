import os
import logging
import subprocess
import threading
import time
import re
import fcntl
import gc
import psutil
from typing import Optional
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
    """
    [PipeWriter]
    """
    def __init__(self, pipe_path, name, is_video=False):
        self.pipe_path = pipe_path
        self.name = name
        self.is_video = is_video
        self.fd = None
        self._broken = False
        self._ensure_pipe()

    def _ensure_pipe(self):
        self._cleanup_file()
        try:
            os.mkfifo(self.pipe_path)
            time.sleep(0.05)
            self.fd = os.open(self.pipe_path, os.O_RDWR | os.O_NONBLOCK)
            try:
                F_SETPIPE_SZ = 1031
                size = 1048576 if self.is_video else 262144
                fcntl.fcntl(self.fd, F_SETPIPE_SZ, size)
            except Exception:
                pass
            logger.debug(f"[{self.name}] Pipe ready: {self.pipe_path}")
            self._broken = False
        except Exception as e:
            logger.error(f"[{self.name}] Failed to create pipe: {e}")
            self._broken = True

    def _cleanup_file(self):
        if os.path.exists(self.pipe_path):
            try:
                os.remove(self.pipe_path)
            except OSError:
                pass

    def write_direct(self, data: bytes):
        if self.fd is None or self._broken: 
            return
        
        # 音频重试10次，视频不重试
        retries = 0 if self.is_video else 10
        
        for i in range(retries + 1):
            try:
                os.write(self.fd, data)
                return 
            except BlockingIOError:
                if self.is_video:
                    return 
                if i < retries:
                    time.sleep(0.002)
                    continue
                return 
            except (BrokenPipeError, OSError) as e:
                if isinstance(e, OSError) and e.errno == 11:
                    if not self.is_video and i < retries:
                        time.sleep(0.002)
                        continue
                    return

                if not self._broken:
                    logger.warning(f"[{self.name}] Pipe Broken! (FFmpeg died?), stopping write.")
                    self._broken = True
                    self.close()
                return

    def close(self):
        if self.fd:
            try:
                os.close(self.fd)
            except:
                pass
            self.fd = None
        self._cleanup_file()

    def __del__(self):
        self.close()


class FFmpegStreamer:
    # 冷却策略
    _global_cooldown_until = 0
    _global_cooldown_step = 10 
    _global_start_lock = threading.Lock()

    def __init__(self, camera_id: str, rtsp_target=None):
        self.camera_id = camera_id
        self.rtsp_url = rtsp_target or f"rtsp://127.0.0.1:{RTSP_PORT}/{camera_id}"
        self.pipe_video = f"/tmp/miloco_video_{camera_id}.pipe"
        self.pipe_audio = f"/tmp/miloco_audio_{camera_id}.pipe"
        self.video_writer: Optional[PipeWriter] = None
        self.audio_writer: Optional[PipeWriter] = None
        self.process: Optional[subprocess.Popen] = None
        
        self._last_health_check = 0 
        
        # [智能看门狗变量]
        self._last_frame_total = 0
        self._last_dup_total = 0
        self._frozen_counter = 0 # 连续完全冻结计数
        
        self._stop_lock = threading.Lock()
        self._should_restart = False

    def start(self, video_codec="hevc"):
        now = time.time()
        if now < FFmpegStreamer._global_cooldown_until:
            wait_time = int(FFmpegStreamer._global_cooldown_until - now)
            # 只有剩余时间较长才打印，避免日志刷屏
            if wait_time > 2:
                logger.warning(f"Start REJECTED: Cooling down for {wait_time}s...")
            return

        if not FFmpegStreamer._global_start_lock.acquire(blocking=False):
            return

        try:
            self.stop() 
            self._last_health_check = time.time()
            self._frozen_counter = 0
            self._last_frame_total = 0
            self._last_dup_total = 0
            self._should_restart = False

            hw_accel = os.getenv("MILOCO_HW_ACCEL", "cpu").lower()
            hw_device = os.getenv("MILOCO_HW_DEVICE", "/dev/dri/renderD128")

            global_args = []
            video_filters = ["setpts=PTS-STARTPTS"]
            video_out_args = []
            common_opts = ['-g', '50', '-bf', '0']

            if hw_accel in ["intel", "amd", "vaapi"]:
                logger.info(f"FFmpeg Start: Hybrid CPU-Decode/GPU-Encode ({self.camera_id})")
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
                    '-profile:v', 'main'
                ] + common_opts

            elif hw_accel in ["nvidia", "nvenc", "cuda"]:
                logger.info(f"FFmpeg Start: NVIDIA ({self.camera_id})")
                video_out_args = ['-c:v', 'h264_nvenc', '-preset', 'p1', '-tune', 'zerolatency'] + common_opts

            else:
                logger.info(f"FFmpeg Start: CPU ({self.camera_id})")
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
                    '-nostats',            
                    '-progress', 'pipe:2', 
                    '-fflags', '+genpts+nobuffer+igndts',
                    '-flags', 'low_delay',
                    
                    # [恢复] 修复H.265码流头，增强兼容性
                    '-bsf:v', 'hevc_mp4toannexb', 
                    
                    '-ec', 'guess_mvs+deblock', 
                    '-err_detect', 'ignore_err',
                    '-analyzeduration', '2000000', 
                    '-probesize', '2000000',
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
                    '-pkt_size', '1316', 
                    '-f', 'rtsp', '-rtsp_transport', 'tcp', '-max_muxing_queue_size', '400',
                    self.rtsp_url,
                ])

                self.process = subprocess.Popen(
                    ffmpeg_cmd, 
                    stdout=subprocess.DEVNULL, 
                    stderr=subprocess.PIPE,
                    text=True,     
                    bufsize=1      
                )
                threading.Thread(target=self._monitor_ffmpeg, daemon=True).start()

            except Exception as e:
                logger.error(f"FFmpeg Launch Failed: {e}")
                self.stop()
        finally:
            FFmpegStreamer._global_start_lock.release()
            
    def _trigger_global_restart(self, reason: str):
        logger.error(f"[Watchdog] {reason}. Cooling down for {FFmpegStreamer._global_cooldown_step}s...")
        self._should_restart = True
        FFmpegStreamer._global_cooldown_until = time.time() + FFmpegStreamer._global_cooldown_step
        FFmpegStreamer._global_cooldown_step = min(FFmpegStreamer._global_cooldown_step + 10, 60)
        if self.process:
            self.process.kill()

    def _monitor_ffmpeg(self):
        if not self.process: return
        
        progress_data = {}
        allow_prefixes = ['Input #', 'Output #', 'Stream #', 'Stream mapping:']
        # 仅针对极少数无法恢复的硬件错误重启
        fatal_hw_errors = [
            'operation not permitted', 'invalid argument',
            'error submitting packet to decoder', 'hardware accelerator failed'
        ]
        
        start_time = time.time()

        for line in self.process.stderr:
            line = line.strip()
            if not line: continue

            if any(err in line.lower() for err in fatal_hw_errors):
                self._trigger_global_restart(f"Fatal Error: '{line}'")
                break 

            if '=' in line and ' ' not in line.split('=', 1)[0]:
                try:
                    k, v = line.split('=', 1)
                    progress_data[k.strip()] = v.strip()
                    
                    if k == 'progress' and v == 'continue':
                        now = time.time()
                        
                        if (now - start_time > 60) and FFmpegStreamer._global_cooldown_step > 10:
                            logger.info("[Watchdog] Stable. Resetting cooldown.")
                            FFmpegStreamer._global_cooldown_step = 10
                        
                        # [智能防误杀逻辑]
                        curr_frame = int(progress_data.get('frame', 0))
                        curr_dup = int(progress_data.get('dup_frames', progress_data.get('dup', 0)))
                        
                        delta_frame = curr_frame - self._last_frame_total
                        delta_dup = curr_dup - self._last_dup_total
                        
                        self._last_frame_total = curr_frame
                        self._last_dup_total = curr_dup
                        
                        # 判断：只有当产生的帧 95% 以上都是重复帧时，才计数为“冻结”
                        # 正常情况：25fps输出，15fps输入 -> 每秒约10个重复帧，占比 40%，不会触发
                        # 卡死情况：25fps输出，0fps输入  -> 每秒25个重复帧，占比 100%，会触发
                        if delta_frame > 0 and (delta_dup / delta_frame) > 0.95:
                            self._frozen_counter += 1
                        else:
                            self._frozen_counter = 0 # 只要有一点新数据，就清零计数器
                        
                        # 连续 20 次检测（约20秒）都是 100% 重复帧，才判定为真死锁
                        if self._frozen_counter > 20: 
                            self._trigger_global_restart(f"Frozen Stream (100% Dups for 20s)")
                            break

                        if (now - self._last_health_check > 60):
                            fps = progress_data.get('fps', 'N/A')
                            speed = progress_data.get('speed', 'N/A')
                            mem = get_memory_usage()
                            logger.info(f"[RTSP] Alive | {fps} fps | {speed}x speed | DupRatio: {delta_dup}/{delta_frame} | Mem: {mem}")
                            self._last_health_check = now
                except:
                    pass
                continue

            if any(line.startswith(p) for p in allow_prefixes):
                logger.info(f"[FFmpeg Info] {line}")
            else:
                is_fatal = any(x in line.lower() for x in ['failed', 'unable', 'no such', 'fatal', 'error'])
                if is_fatal:
                    # 只记录不处理，防止误杀
                    pass

        if self.process:
            self.process.poll()
            
        if self._should_restart:
            self.stop() 

    def stop(self):
        with self._stop_lock:
            if self.video_writer: 
                self.video_writer.close(); self.video_writer = None
            if self.audio_writer: 
                self.audio_writer.close(); self.audio_writer = None
            
            if self.process:
                try:
                    self.process.terminate()
                    try:
                        self.process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        logger.warning(f"FFmpeg hung, force killing...")
                        self.process.kill()
                        self.process.wait(timeout=1)
                except Exception:
                    pass
                finally:
                    self.process = None
            
            time.sleep(0.5)
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
