import os
import logging
import subprocess
import threading
import time
import re
import fcntl
import gc  # [新增] 引入垃圾回收模块
from typing import Optional
from miloco_server.config import RTSP_PORT

logger = logging.getLogger(__name__)


class PipeWriter:
    """
    [PipeWriter] 阻塞式 I/O
    """
    def __init__(self, pipe_path, name, is_video=False):
        self.pipe_path = pipe_path
        self.name = name
        self.is_video = is_video
        self.fd = None
        self._ensure_pipe()

    def _ensure_pipe(self):
        try:
            if os.path.exists(self.pipe_path):
                try:
                    os.remove(self.pipe_path)
                except OSError:
                    pass
            os.mkfifo(self.pipe_path)
            flags = os.O_RDWR
            self.fd = os.open(self.pipe_path, flags)
            try:
                F_SETPIPE_SZ = 1031
                # 视频 1MB, 音频 128KB
                size = 1048576 if self.is_video else 131072
                fcntl.fcntl(self.fd, F_SETPIPE_SZ, size)
            except Exception:
                pass
            # 简化日志: 只有 debug 模式才打印
            logger.debug(f"[{self.name}] Pipe opened: {self.pipe_path}")
            return True
        except Exception as e:
            logger.error(f"[{self.name}] Pipe creation error: {e}")
            return False

    def write_direct(self, data: bytes):
        if self.fd is None: return
        try:
            os.write(self.fd, data)
        except OSError:
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


class FFmpegStreamer:
    def __init__(self, camera_id: str, rtsp_target=None):
        self.camera_id = camera_id
        self.rtsp_url = rtsp_target or f"rtsp://127.0.0.1:{RTSP_PORT}/{camera_id}"
        self.pipe_video = f"/tmp/miloco_video_{camera_id}.pipe"
        self.pipe_audio = f"/tmp/miloco_audio_{camera_id}.pipe"
        self.video_writer: Optional[PipeWriter] = None
        self.audio_writer: Optional[PipeWriter] = None
        self.process: Optional[subprocess.Popen] = None
        self._last_health_check = time.time()

    def start(self, video_codec="hevc"):
        self.stop()  # 启动前彻底清理旧进程

        hw_accel = os.getenv("MILOCO_HW_ACCEL", "cpu").lower()
        hw_device = os.getenv("MILOCO_HW_DEVICE", "/dev/dri/renderD128")

        global_args = []
        video_filters = ["setpts=PTS-STARTPTS"]
        video_out_args = []
        common_opts = ['-g', '50', '-bf', '0']

        # --- 混合架构 (Stable Hybrid) ---
        # 移除了不稳定的 scale_vaapi，解决 -38 报错和内存泄露
        if hw_accel in ["intel", "amd", "vaapi"]:
            logger.info(f"FFmpeg Mode: Hybrid VAAPI ({self.camera_id})")

            global_args = [
                '-init_hw_device', f'vaapi=va:{hw_device}',
                '-filter_hw_device', 'va'
            ]
            
            # 仅做格式转换和上传，不做缩放，这是最稳的
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
            video_out_args = ['-c:v', 'libx264', '-preset', 'ultrafast', '-tune', 'zerolatency', '-profile:v',
                              'baseline'] + common_opts

        video_filter_str = ",".join(video_filters)

        # 音频：保持防拖尾
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

                '-analyzeduration', '1000000',
                '-probesize', '1000000',
                '-err_detect', 'ignore_err',
            ]

            ffmpeg_cmd.extend(global_args)

            ffmpeg_cmd.extend([
                '-thread_queue_size', '64',
                '-f', video_codec,
                '-use_wallclock_as_timestamps', '1',
                '-i', self.pipe_video,

                '-thread_queue_size', '512',
                '-f', 's16le', '-ar', '16000', '-ac', '1',
                '-use_wallclock_as_timestamps', '1',
                '-i', self.pipe_audio,

                '-map', '0:v', '-map', '1:a',
                '-vf', video_filter_str,
                '-af', audio_filter_chain,

                *video_out_args,
                '-c:a', 'aac', '-ar', '16000', '-b:a', '64k',
                
                # [关键修复] 补回 HomeKit 4G 观看补丁
                '-pkt_size', '1316',

                '-f', 'rtsp', '-rtsp_transport', 'tcp', '-max_muxing_queue_size', '400',
                self.rtsp_url,
            ])

            self.process = subprocess.Popen(ffmpeg_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            threading.Thread(target=self._monitor_ffmpeg, daemon=True).start()

        except Exception as e:
            logger.error(f"Start failed: {e}")
            self.stop()

    def _monitor_ffmpeg(self):
        if not self.process: return
        pattern = re.compile(r'frame=\s*(\d+).*fps=\s*([\d.]+).*speed=\s*([\d.]+)x')

        ignore_keywords = [
            'pps', 'nalu', 'header', 'buffer', 'frame rate', 'unspecified',
            'params', 'ref with poc', 'too many bits', 'last message repeated',
            'deprecated', 'pixel format', 'function not implemented'
        ]

        while self.process and self.process.poll() is None:
            try:
                line = self.process.stderr.readline()
                if not line: break
                l = line.decode(errors='ignore').strip()
                if not l: continue

                if "frame=" in l:
                    now = time.time()
                    if (now - self._last_health_check > 60):  # 降低心跳日志频率
                        match = pattern.search(l)
                        if match:
                            logger.info(f"[RTSP] Active... {match.group(0)}")
                            self._last_health_check = now
                else:
                    is_fatal = any(x in l.lower() for x in ['failed', 'unable', 'no such', 'fatal', 'error'])
                    is_ignored = any(k in l.lower() for k in ignore_keywords)

                    if is_fatal and not is_ignored:
                        logger.error(f"[FFmpeg Error] {l}")

            except Exception:
                break

        if self.process:
            ret = self.process.poll()
            if ret is not None and ret != 0:
                logger.error(f"FFmpeg exited unexpectedly code: {ret}")

    def stop(self):
        # 1. 优先关闭管道
        if self.video_writer: self.video_writer.close(); self.video_writer = None
        if self.audio_writer: self.audio_writer.close(); self.audio_writer = None
        
        # 2. 强力终止进程
        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=3)
            except:
                try:
                    self.process.kill()  # 强制 Kill
                    self.process.wait()
                except:
                    pass
            self.process = None
            
        # 3. [关键修复] 显式垃圾回收，防止内存堆积
        gc.collect()

    def push_audio_raw(self, data: bytes):
        if self.audio_writer: self.audio_writer.write_direct(data)

    def push_video(self, data: bytes, seq: int, is_i_frame: bool = False):
        if self.video_writer:
            self.video_writer.write_direct(data)
