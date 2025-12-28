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

def get_memory_usage():
    try:
        process = psutil.Process(os.getpid())
        mem = process.memory_info().rss / 1024 / 1024
        return f"{mem:.1f} MB"
    except:
        return "N/A"

class PipeWriter:
    """
    [PipeWriter] 极低延迟版：小缓存 + 非阻塞
    """
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
            
            # 1. 使用非阻塞模式，防止死锁和内存溢出
            self.fd = os.open(self.pipe_path, os.O_RDWR | os.O_NONBLOCK)
            try:
                F_SETPIPE_SZ = 1031
                # [核心修改] 只有砍掉缓存，才能物理上消除延迟
                # 视频: 256KB (足够容纳一个 I 帧，但存不下1秒的积压)
                # 音频: 16KB (约 0.5s，逼迫数据实时流动)
                size = 262144 if self.is_video else 16384
                fcntl.fcntl(self.fd, F_SETPIPE_SZ, size)
            except Exception:
                pass
            logger.debug(f"[{self.name}] Pipe ready: {self.pipe_path}")
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
            # 管道满了，丢弃最新数据。
            # 在小缓存模式下，这意味着"以前的数据"被保留，"新数据"被丢弃？
            # 不，对于实时流，丢包比延迟好。保持管道不满才是关键。
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
    _start_lock = threading.Lock()
    # 冷却策略
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

    def start(self, video_codec="hevc"):
        # 简单的冷却检查
        if time.time() < FFmpegStreamer._global_cooldown_until:
            return
            
        if not self._start_lock.acquire(blocking=False):
            return

        try:
            self.stop() 
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
                    '-profile:v', 'main'
                ] + common_opts
            elif hw_accel in ["nvidia", "nvenc", "cuda"]:
                logger.info(f"FFmpeg Mode: NVIDIA ({self.camera_id})")
                video_out_args = ['-c:v', 'h264_nvenc', '-preset', 'p1', '-tune', 'zerolatency'] + common_opts
            else:
                logger.info(f"FFmpeg Mode: CPU ({self.camera_id})")
                video_out_args = ['-c:v', 'libx264', '-preset', 'ultrafast', '-tune', 'zerolatency', '-profile:v', 'baseline'] + common_opts

            video_filter_str = ",".join(video_filters)
            # 音频同步：允许动态调整，防止断音
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
                    
                    # [修复崩溃] 禁止滤镜重置
                    '-reinit_filter', '0',
                    
                    # [极速启动] 减少探测时间，进一步降低起步延迟
                    '-analyzeduration', '400000', 
                    '-probesize', '200000',       
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
                    # 移除音频绝对时间戳，配合小缓存解决延迟和断音
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

                self.process = subprocess.Popen(
                    ffmpeg_cmd, 
                    stdout=subprocess.DEVNULL, 
                    stderr=subprocess.PIPE,
                    text=True,  
                    bufsize=1   
                )
                threading.Thread(target=self._monitor_ffmpeg, daemon=True).start()

            except Exception as e:
                logger.error(f"FFmpeg start failed: {e}")
                self.stop()
        finally:
            self._start_lock.release()

    def _trigger_restart(self, reason):
        logger.error(f"[Watchdog] {reason}. Cooling down.")
        FFmpegStreamer._global_cooldown_until = time.time() + FFmpegStreamer._global_cooldown_step
        # 简单增加步进
        FFmpegStreamer._global_cooldown_step = min(FFmpegStreamer._global_cooldown_step + 10, 60)
        self.stop()

    def _monitor_ffmpeg(self):
        if not self.process: return
        
        fd = self.process.stderr.fileno()
        os.set_blocking(fd, False)
        
        pattern = re.compile(r'frame=\s*(\d+).*fps=\s*([\d.]+).*speed=\s*([\d.]+)x')
        ignore_keywords = ['pps', 'nalu', 'header', 'buffer', 'frame rate', 'unspecified', 'params', 'last message repeated', 'deprecated']

        # 简单看门狗：只监视是否存活和严重错误
        # 不再监视 Dup，因为那是正常现象
        while not self._stop_event.is_set():
            if self.process.poll() is not None:
                logger.error(f"FFmpeg exited (Code: {self.process.returncode})")
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
                        if (now - self._last_health_check > 60): 
                            match = pattern.search(l)
                            if match:
                                mem = get_memory_usage()
                                logger.info(f"[RTSP] Alive | {match.group(0)} | Mem: {mem}")
                                self._last_health_check = now
                                # 成功运行后重置冷却
                                FFmpegStreamer._global_cooldown_step = 10
                    else:
                        is_fatal = any(x in l.lower() for x in ['failed', 'unable', 'no such', 'fatal', 'error'])
                        is_ignored = any(k in l.lower() for k in ignore_keywords)
                        if is_fatal and not is_ignored:
                            logger.error(f"[FFmpeg Error] {l}")
                            # 遇到严重配置错误，触发重启保护
                            if "invalid argument" in l.lower() or "function not implemented" in l.lower():
                                self._trigger_restart(f"Fatal Config Error: {l}")
                                return
            except Exception:
                break
        
        self.stop()

    def stop(self):
        self._stop_event.set()
        
        if self.video_writer: 
            self.video_writer.close(); self.video_writer = None
        if self.audio_writer: 
            self.audio_writer.close(); self.audio_writer = None
            
        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=2)
            except:
                try:
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
