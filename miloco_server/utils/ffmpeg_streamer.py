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
    def __init__(self, pipe_path, name, owner, is_video=False):
        self.pipe_path = pipe_path
        self.name = name
        self.owner = owner
        self.is_video = is_video
        self.fd = None
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
                # [极低延迟模式] 
                # 视频: 256KB (约 3-5 帧，不允许积压)
                # 音频: 16KB (瞬时转发)
                # 这是一个“宁可丢包，不可延迟”的设置
                size = 262144 if self.is_video else 16384
                fcntl.fcntl(self.fd, F_SETPIPE_SZ, size)
            except:
                pass
            return True
        except Exception as e:
            logger.error(f"[{self.name}] Pipe error: {e}")
            return False

    def write_direct(self, data: bytes):
        if self.fd is None: return
        try:
            os.write(self.fd, data)
        except BlockingIOError:
            # [丢包策略]
            # 为了保证实时性，管道满时直接丢弃当前包，而不是重启
            # 只有当持续堵塞导致看门狗超时，才会在上层逻辑处理
            pass 
        except Exception:
            self.close()

    def close(self):
        if self.fd:
            try: os.close(self.fd)
            except: pass
            self.fd = None
        if os.path.exists(self.pipe_path):
            try: os.remove(self.pipe_path)
            except: pass

    def __del__(self):
        self.close()


class FFmpegStreamer:
    _start_lock = threading.Lock()
    _global_cooldown_until = 0

    def __init__(self, camera_id: str, rtsp_target=None):
        self.camera_id = camera_id
        self.rtsp_url = rtsp_target or f"rtsp://127.0.0.1:{RTSP_PORT}/{camera_id}"
        self.pipe_video = f"/tmp/miloco_video_{camera_id}.pipe"
        self.pipe_audio = f"/tmp/miloco_audio_{camera_id}.pipe"
        self.video_writer: Optional[PipeWriter] = None
        self.audio_writer: Optional[PipeWriter] = None
        self.process: Optional[subprocess.Popen] = None
        self._stop_event = threading.Event()
        self._last_log_time = 0

    def _force_kill_zombies(self):
        try:
            cmd = f"pgrep -f 'ffmpeg.*{self.camera_id}'"
            subprocess.run(cmd, shell=True, stdout=subprocess.DEVNULL)
        except:
            pass

    def start(self, video_codec="hevc"):
        if time.time() < FFmpegStreamer._global_cooldown_until:
            return
        if not self._start_lock.acquire(blocking=False):
            return

        try:
            self.stop()
            self._force_kill_zombies()
            time.sleep(0.5)
            self._stop_event.clear()

            hw_accel = os.getenv("MILOCO_HW_ACCEL", "cpu").lower()
            hw_device = os.getenv("MILOCO_HW_DEVICE", "/dev/dri/renderD128")

            # 混合模式架构：CPU解码 -> 上传 -> GPU编码
            global_args = []
            video_filters = ["setpts=PTS-STARTPTS"] 
            video_out_args = []
            common_opts = ['-bf', '0']

            if hw_accel in ["intel", "amd", "vaapi"]:
                logger.info(f"FFmpeg Mode: Hybrid Low Latency ({self.camera_id})")
                global_args = [
                    '-init_hw_device', f'vaapi=va:{hw_device}',
                    '-filter_hw_device', 'va'
                ]
                video_filters.extend(['format=nv12', 'hwupload'])
                
                # [低延迟编码参数]
                video_out_args = [
                    '-c:v', 'h264_vaapi',
                    '-g', '25',
                    '-rc_mode', 'CQP',    
                    '-global_quality', '28', 
                    '-profile:v', 'main',
                    '-async_depth', '1' # 禁止显卡缓冲，来一帧编一帧
                ] + common_opts

            elif hw_accel in ["nvidia", "nvenc", "cuda"]:
                logger.info(f"FFmpeg Mode: Hybrid Low Latency (NVENC) ({self.camera_id})")
                video_out_args = [
                    '-c:v', 'h264_nvenc', 
                    '-preset', 'p1',       # 最快预设
                    '-tune', 'zerolatency', # 零延迟调优
                    '-delay', '0',
                    '-g', '25'
                ] + common_opts
            else:
                logger.info(f"FFmpeg Mode: CPU Low Latency ({self.camera_id})")
                video_out_args = [
                    '-c:v', 'libx264', 
                    '-preset', 'ultrafast', 
                    '-tune', 'zerolatency', 
                    '-g', '25'
                ] + common_opts

            video_filter_str = ",".join(video_filters)
            
            # [低延迟音频] async=1: 只做微小调整，不引入大缓冲
            audio_filter_chain = "aresample=async=1:min_hard_comp=0.100000:first_pts=0"

            try:
                self.video_writer = PipeWriter(self.pipe_video, "Video", self, is_video=True)
                self.audio_writer = PipeWriter(self.pipe_audio, "Audio", self, is_video=False)

                ffmpeg_cmd = [
                    'ffmpeg', '-y', '-hide_banner', '-loglevel', 'warning', '-stats',
                    
                    # [极速启动 & 零缓冲]
                    '-fflags', '+genpts+nobuffer+igndts', # 禁用所有 FFmpeg 内部缓冲
                    '-flags', 'low_delay',                # 开启低延迟标志
                    '-reinit_filter', '0',                # 避免滤镜重置带来的卡顿
                    
                    '-analyzeduration', '200000', # 0.2秒探测
                    '-probesize', '200000',
                ]
                
                ffmpeg_cmd.extend(global_args)
                
                ffmpeg_cmd.extend([
                    # [小队列] 强制快速处理，堆积即丢弃
                    '-thread_queue_size', '64',
                    '-f', video_codec,
                    '-use_wallclock_as_timestamps', '1',
                    '-i', self.pipe_video,

                    '-thread_queue_size', '64',
                    '-f', 's16le', '-ar', '16000', '-ac', '1',
                    # 音频依然使用采样率时间戳，保证连续性
                    '-i', self.pipe_audio,

                    '-map', '0:v', '-map', '1:a',
                    '-vf', video_filter_str,
                    '-af', audio_filter_chain,

                    *video_out_args,
                    
                    '-c:a', 'aac', '-ar', '16000', '-b:a', '32k',
                    
                    # UDP + 小队列输出
                    '-f', 'rtsp', 
                    '-rtsp_transport', 'udp', 
                    '-pkt_size', '1316', 
                    '-max_muxing_queue_size', '100', # 输出队列只要满 100 个包就丢弃，防止延迟累积
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
        if time.time() < FFmpegStreamer._global_cooldown_until: return
        logger.warning(f"[Watchdog] {reason}. Restarting...")
        FFmpegStreamer._global_cooldown_until = time.time() + 5
        threading.Thread(target=self.start, daemon=True).start()

    def _monitor_ffmpeg(self):
        if not self.process: return
        fd = self.process.stderr.fileno()
        os.set_blocking(fd, False)
        
        while not self._stop_event.is_set():
            if self.process.poll() is not None:
                if self.process.returncode not in [0, -9, 234, 111]:
                    logger.error(f"FFmpeg exited: {self.process.returncode}")
                break

            try:
                if select.select([fd], [], [], 1.0)[0]:
                    line = self.process.stderr.readline()
                    if line and "frame=" in line:
                        if time.time() - self._last_log_time > 60:
                            logger.info(f"[RTSP] Alive | {line.strip()}")
                            self._last_log_time = time.time()
                    elif "error" in line.lower() and "invalid argument" in line.lower():
                        self._trigger_restart(f"Fatal Config: {line}")
                        return
            except:
                break
        self.stop()

    def stop(self):
        self._stop_event.set()
        if self.video_writer: self.video_writer.close(); self.video_writer = None
        if self.audio_writer: self.audio_writer.close(); self.audio_writer = None
        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=1)
            except:
                try: self.process.kill()
                except: pass
            self.process = None
        gc.collect()

    def push_audio_raw(self, data: bytes):
        if self.audio_writer: self.audio_writer.write_direct(data)

    def push_video(self, data: bytes, seq: int, is_i_frame: bool = False):
        if self.video_writer: self.video_writer.write_direct(data)

    def __del__(self):
        try: self.stop()
        except: pass
