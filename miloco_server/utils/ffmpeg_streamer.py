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
    """获取当前进程内存占用，用于监控泄漏"""
    try:
        process = psutil.Process(os.getpid())
        mem = process.memory_info().rss / 1024 / 1024
        return f"{mem:.1f} MB"
    except:
        return "N/A"

class PipeWriter:
    """
    [PipeWriter] 稳定版：非阻塞 I/O + 异常状态记录
    """
    def __init__(self, pipe_path, name, is_video=False):
        self.pipe_path = pipe_path
        self.name = name
        self.is_video = is_video
        self.fd = None
        self._broken = False # 状态标记，防止日志刷屏
        self._ensure_pipe()

    def _ensure_pipe(self):
        self.close() 
        try:
            if os.path.exists(self.pipe_path):
                os.remove(self.pipe_path)
            os.mkfifo(self.pipe_path)
            
            # 使用非阻塞模式打开，这是防止 Python 线程卡死(导致内存不释放)的关键
            self.fd = os.open(self.pipe_path, os.O_RDWR | os.O_NONBLOCK)
            try:
                F_SETPIPE_SZ = 1031
                size = 1048576 if self.is_video else 131072
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
            # 管道满了，丢弃数据。这是正常的背压保护，不打印日志以免刷屏
            pass 
        except (BrokenPipeError, OSError) as e:
            # [关键诊断日志] 管道破裂意味着 FFmpeg 进程可能已经挂了
            if not self._broken:
                logger.warning(f"[{self.name}] Pipe broken/disconnected: {e}")
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
    # 全局锁，防止多次点击导致并发冲突
    _start_lock = threading.Lock()

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
        if not self._start_lock.acquire(blocking=False):
            logger.warning(f"Start request ignored: Another start sequence is in progress.")
            return

        try:
            self.stop() # 启动前先彻底清理
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
            
            # [低延迟音频配置] async=1 + min_hard_comp 允许动态对齐，解决断音和延迟
            audio_filter_chain = "aresample=async=1:min_hard_comp=0.100000:first_pts=0"

            try:
                self.video_writer = PipeWriter(self.pipe_video, "Video", is_video=True)
                self.audio_writer = PipeWriter(self.pipe_audio, "Audio", is_video=False)

                ffmpeg_cmd = [
                    'ffmpeg', '-y',
                    '-hide_banner',
                    '-loglevel', 'warning', 
                    '-stats',  # 必须开启，否则无法监控进度

                    '-fflags', '+genpts+nobuffer+igndts',
                    '-flags', 'low_delay',

                    '-analyzeduration', '1000000',
                    '-probesize', '1000000',
                    '-err_detect', 'ignore_err',
                ]

                ffmpeg_cmd.extend(global_args)

                ffmpeg_cmd.extend([
                    '-thread_queue_size', '128',
                    '-f', video_codec,
                    '-use_wallclock_as_timestamps', '1', # 视频使用绝对时钟，保证画面低延迟
                    '-i', self.pipe_video,

                    '-thread_queue_size', '512',
                    '-f', 's16le', '-ar', '16000', '-ac', '1',
                    '-use_wallclock_as_timestamps', '1', # 音频也使用绝对时钟，通过 aresample 解决断续
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

    def _monitor_ffmpeg(self):
        if not self.process: return
        
        # 使用 select 监听 stderr，这是防止僵死和内存泄露的最稳健方法
        fd = self.process.stderr.fileno()
        os.set_blocking(fd, False)
        
        pattern = re.compile(r'frame=\s*(\d+).*fps=\s*([\d.]+).*speed=\s*([\d.]+)x')
        # 忽略常规的启动警告
        ignore_keywords = ['pps', 'nalu', 'header', 'buffer', 'frame rate', 'unspecified', 'params', 'last message repeated', 'deprecated']

        while not self._stop_event.is_set():
            if self.process.poll() is not None:
                logger.error(f"FFmpeg process exited unexpectedly (Code: {self.process.returncode})")
                break

            try:
                # 1秒超时，不阻塞线程
                ready = select.select([fd], [], [], 1.0)
                if ready[0]:
                    line = self.process.stderr.readline()
                    if not line: continue
                    
                    l = line.strip()
                    if not l: continue

                    if "frame=" in l:
                        now = time.time()
                        # [心跳日志] 每 60 秒打印一次，用于长期观察内存和存活状态
                        if (now - self._last_health_check > 60): 
                            match = pattern.search(l)
                            if match:
                                mem = get_memory_usage()
                                logger.info(f"[RTSP] Alive | {match.group(0)} | Mem: {mem}")
                                self._last_health_check = now
                    else:
                        # [错误日志] 只打印严重错误
                        is_fatal = any(x in l.lower() for x in ['failed', 'unable', 'no such', 'fatal', 'error'])
                        is_ignored = any(k in l.lower() for k in ignore_keywords)
                        if is_fatal and not is_ignored:
                            logger.error(f"[FFmpeg Error] {l}")
            except Exception:
                break
        
        # 线程退出时执行清理
        self.stop()

    def stop(self):
        self._stop_event.set() # 信号量通知监控线程退出
        
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
