import os
import logging
import subprocess
import threading
import queue
import time
import re
import fcntl
from typing import Optional
from miloco_server.config import RTSP_PORT

logger = logging.getLogger(__name__)


class PipeWriter:
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

            # 阻塞模式，保证数据不丢
            flags = os.O_RDWR
            self.fd = os.open(self.pipe_path, flags)

            try:
                F_SETPIPE_SZ = 1031
                # 视频给足 1MB，音频 64KB
                fcntl.fcntl(self.fd, F_SETPIPE_SZ, 1024 * 1024 if self.is_video else 65536)
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

    def _get_video_output_args(self):
        return [
            '-c:v', 'libx264', '-preset', 'ultrafast', '-tune', 'zerolatency',
            '-g', '50', '-bf', '0', '-profile:v', 'baseline'
        ]

    def start(self, video_codec="hevc"):
        self.stop()

        try:
            self.video_writer = PipeWriter(self.pipe_video, "Video", is_video=True)
            self.audio_writer = PipeWriter(self.pipe_audio, "Audio", is_video=False)

            video_out_args = self._get_video_output_args()

            ffmpeg_cmd = [
                'ffmpeg', '-y', '-v', 'info', '-hide_banner',
                '-fflags', '+genpts+nobuffer+igndts',
                '-flags', 'low_delay',

                # [核心修正 1] 移除 -r 25！
                # [核心修正 2] 启用 wallclock，让 FFmpeg 使用 Python 写入时的系统时间作为 PTS

                # --- Video Input ---
                '-thread_queue_size', '32',
                '-f', video_codec,
                # '-r', '25',  <-- 罪魁祸首已删除！
                '-use_wallclock_as_timestamps', '1',
                '-i', self.pipe_video,

                # --- Audio Input ---
                '-thread_queue_size', '32',
                '-f', 's16le', '-ar', '16000', '-ac', '1',
                '-use_wallclock_as_timestamps', '1',
                '-i', self.pipe_audio,

                '-map', '0:v', '-map', '1:a',

                # --- 过滤器 ---
                # 1. 视频和音频都重置 PTS，使它们从 0 开始
                # 2. aresample=async=1: 这是一个神器。
                #    因为它发现视频是 19.6fps 而不是标准的 25fps，音频时间轴会和视频微小错位。
                #    这个参数会拉伸/压缩音频来强行匹配视频的时间轴。
                '-vf', 'setpts=PTS-STARTPTS',
                '-af', 'aresample=async=1:first_pts=0',

                # Output
                *video_out_args,
                '-c:a', 'aac', '-ar', '16000', '-b:a', '32k',

                '-f', 'rtsp', '-rtsp_transport', 'tcp', '-max_muxing_queue_size', '400',
                self.rtsp_url,
            ]

            logger.info(f"Starting FFmpeg (FPS Correction Mode) for {self.camera_id}...")
            self.process = subprocess.Popen(
                ffmpeg_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
            )
            threading.Thread(target=self._monitor_ffmpeg, daemon=True).start()

        except Exception as e:
            logger.error(f"Start failed: {e}")
            self.stop()

    def _monitor_ffmpeg(self):
        if not self.process: return
        pattern = re.compile(r'frame=\s*(\d+).*fps=\s*([\d.]+).*speed=\s*([\d.]+)x')
        try:
            for line in self.process.stderr:
                l = line.decode(errors='ignore').strip()
                if "frame=" in l:
                    now = time.time()
                    match = pattern.search(l)
                    if match and (now - self._last_health_check > 30):
                        logger.info(f"[FFmpeg Status] {l}")
                        self._last_health_check = now
        except Exception:
            pass

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