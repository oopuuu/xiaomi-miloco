import os
import logging
import subprocess
import threading
import time
import re
import fcntl
from typing import Optional
from miloco_server.config import RTSP_PORT

logger = logging.getLogger(__name__)


class PipeWriter:
    """
    [PipeWriter] 阻塞式 I/O
    保持不变。
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
                # 视频 1MB, 音频 64KB
                size = 1048576 if self.is_video else 65536
                fcntl.fcntl(self.fd, F_SETPIPE_SZ, size)
            except Exception:
                pass
            logger.info(f"[{self.name}] Pipe opened: {self.pipe_path}")
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
        self._startup_success = False

    def start(self, video_codec="hevc"):
        self.stop()
        self._startup_success = False

        hw_accel = os.getenv("MILOCO_HW_ACCEL", "cpu").lower()
        hw_device = os.getenv("MILOCO_HW_DEVICE", "/dev/dri/renderD128")

        global_args = []
        video_filters = ["setpts=PTS-STARTPTS"]
        video_out_args = []
        common_opts = ['-g', '50', '-bf', '0']

        # --- 硬件加速配置 (混合模式: 软解->硬编) ---
        if hw_accel in ["intel", "amd", "vaapi"]:
            logger.info(f"Using Hybrid HW Pipeline: Soft Decode -> VAAPI Encode ({hw_device})")

            # 1. 全局初始化设备 (必须保留)
            global_args = [
                '-init_hw_device', f'vaapi=va:{hw_device}',
                '-filter_hw_device', 'va'
            ]

            # 2. 滤镜链: 必须包含 hwupload!
            # 因为输入是软解(内存)，必须上传到显存才能给 h264_vaapi 编码
            video_filters.extend(['format=nv12', 'hwupload'])

            # 3. 编码参数 (保持 CQP)
            video_out_args = [
                                 '-c:v', 'h264_vaapi',
                                 '-async_depth', '1',
                                 '-rc_mode', 'CQP',
                                 '-global_quality', '25'
                             ] + common_opts

        elif hw_accel in ["nvidia", "nvenc", "cuda"]:
            logger.info("Using HW Accel: NVIDIA NVENC")
            video_out_args = ['-c:v', 'h264_nvenc', '-preset', 'p1', '-tune', 'zerolatency'] + common_opts

        else:
            logger.info("Using SW Encoding: libx264")
            video_out_args = ['-c:v', 'libx264', '-preset', 'ultrafast', '-tune', 'zerolatency', '-profile:v',
                              'baseline'] + common_opts

        video_filter_str = ",".join(video_filters)

        # [音频策略：净空模式]
        # 没有任何改变音色的滤镜，只做最基础的同步和断流保护。
        # min_hard_comp=0.1: 专门对付"滋滋滋"拖尾。
        audio_filter_chain = "aresample=async=1:min_hard_comp=0.100000:first_pts=0"

        try:
            self.video_writer = PipeWriter(self.pipe_video, "Video", is_video=True)
            self.audio_writer = PipeWriter(self.pipe_audio, "Audio", is_video=False)

            ffmpeg_cmd = [
                'ffmpeg', '-y', '-v', 'info', '-hide_banner',
                '-fflags', '+genpts+nobuffer+igndts',
                '-flags', 'low_delay',

                # [关键恢复] 缓冲区增加到 2MB
                # 软解对缓冲区没那么敏感，2MB 足够快速启动且不报错
                '-analyzeduration', '2000000',
                '-probesize', '2000000',
                '-err_detect', 'ignore_err',
            ]

            # 插入全局参数
            ffmpeg_cmd.extend(global_args)

            ffmpeg_cmd.extend([
                # --- Video Input (回归软解) ---
                # 移除了 -hwaccel vaapi，因为 Pipe 输入用硬解极不稳定
                '-thread_queue_size', '64',
                '-f', video_codec,
                '-use_wallclock_as_timestamps', '1',
                '-i', self.pipe_video,

                # --- Audio Input ---
                '-thread_queue_size', '64',
                '-f', 's16le', '-ar', '16000', '-ac', '1',
                '-use_wallclock_as_timestamps', '1',
                '-i', self.pipe_audio,

                '-map', '0:v', '-map', '1:a',

                # --- Filters ---
                '-vf', video_filter_str,
                '-af', audio_filter_chain,

                # --- Output ---
                *video_out_args,
                '-c:a', 'aac', '-ar', '16000', '-b:a', '64k',
                '-f', 'rtsp', '-rtsp_transport', 'tcp', '-max_muxing_queue_size', '400',
                self.rtsp_url,
            ])

            logger.info(f"Starting FFmpeg (Hybrid Mode + Clean Audio) for {self.camera_id}...")

            self.process = subprocess.Popen(ffmpeg_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            threading.Thread(target=self._monitor_ffmpeg, daemon=True).start()

        except Exception as e:
            logger.error(f"Start failed: {e}")
            self.stop()

    def _monitor_ffmpeg(self):
        if not self.process: return
        pattern = re.compile(r'frame=\s*(\d+).*fps=\s*([\d.]+).*speed=\s*([\d.]+)x')
        while self.process and self.process.poll() is None:
            try:
                line = self.process.stderr.readline()
                if not line: break
                l = line.decode(errors='ignore').strip()
                if not l: continue
                if "frame=" in l:
                    self._startup_success = True
                    now = time.time()
                    match = pattern.search(l)
                    if match and (now - self._last_health_check > 30):
                        logger.info(f"[FFmpeg Status] {l}")
                        self._last_health_check = now
                else:
                    is_fatal = any(x in l.lower() for x in ['failed', 'unable', 'no such', 'fatal'])
                    if is_fatal:
                        logger.error(f"[FFmpeg Error] {l}")
                    elif not self._startup_success and "PPS" not in l and "NALU" not in l:
                        logger.info(f"[FFmpeg Startup] {l}")
            except Exception:
                break
        if self.process:
            ret = self.process.poll()
            if ret is not None and ret != 0: logger.error(f"FFmpeg exited unexpectedly with code {ret}")

    def stop(self):
        if self.video_writer: self.video_writer.close(); self.video_writer = None
        if self.audio_writer: self.audio_writer.close(); self.audio_writer = None
        if self.process:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except:
                self.process.kill()
            self.process = None

    def push_audio_raw(self, data: bytes):
        if self.audio_writer: self.audio_writer.write_direct(data)

    def push_video(self, data: bytes, seq: int, is_i_frame: bool = False):
        if self.video_writer:
            self.video_writer.write_direct(data)