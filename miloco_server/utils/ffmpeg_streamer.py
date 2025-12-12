import os
import logging
import subprocess
import threading
import queue
import time
from typing import Optional
from miloco_server.config import RTSP_PORT

logger = logging.getLogger(__name__)


class PipeWriter(threading.Thread):
    def __init__(self, pipe_path, name):
        super().__init__(daemon=True)
        self.pipe_path = pipe_path
        self.name = name
        self.queue = queue.Queue(maxsize=300)
        self.fd = None
        self.running = True
        self._ensure_pipe()

    def _ensure_pipe(self):
        try:
            if os.path.exists(self.pipe_path):
                try:
                    os.remove(self.pipe_path)
                except:
                    pass
            os.mkfifo(self.pipe_path)
            self.fd = os.open(self.pipe_path, os.O_RDWR)
            logger.info(f"[{self.name}] Pipe opened: {self.pipe_path}")
        except Exception as e:
            logger.error(f"[{self.name}] Pipe error: {e}")
            self.running = False

    def write(self, data):
        if not self.running: return

        # [监控] 检查队列深度
        q_size = self.queue.qsize()

        # 如果队列长期维持在高位 (>200)，说明下游(FFmpeg)处理太慢，延迟正在积累
        if q_size > 200:
            # logger.warning(f"[{self.name}] High Buffer Warning: {q_size}/300")
            pass

        if self.queue.full():
            try:
                # [关键行为监控] 打印这个日志！
                # 如果这个日志每隔几秒就出现一次，说明你的网络带宽不足或编码太慢，
                # 但同时也说明"漏桶机制"正在工作，强制把延迟“倒掉”了。
                # 只要有这个机制，延迟就绝对不会无限累积。
                logger.warning(f"[{self.name}] Buffer FULL! Dropping all old packets to fix latency.")
                with self.queue.mutex:
                    self.queue.queue.clear()
            except:
                pass

        try:
            self.queue.put_nowait(data)
        except:
            pass

    def run(self):
        while self.running:
            try:
                data = self.queue.get(timeout=1.0)
                if self.fd: os.write(self.fd, data)
            except queue.Empty:
                continue
            except OSError:
                break
            except:
                break
        self.close()

    def close(self):
        self.running = False
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
        self.video_writer = None
        self.audio_writer = None
        self.process = None
        self._last_video_seq = -1

    def start(self, video_codec="hevc"):
        self.stop()
        self._last_video_seq = -1

        self.video_writer = PipeWriter(self.pipe_video, "Video")
        self.audio_writer = PipeWriter(self.pipe_audio, "Audio")
        self.video_writer.start()
        self.audio_writer.start()

        # [V9.2 最终版: 移除拦截 + 增强容错]
        ffmpeg_cmd = [
            'ffmpeg', '-y', '-v', 'info', '-hide_banner',

            '-use_wallclock_as_timestamps', '1',
            '-fflags', '+genpts+nobuffer+igndts',
            '-flags', 'low_delay',
            # [新增] 忽略解码初期的错误，直到同步
            '-err_detect', 'ignore_err',
            '-analyzeduration', '5000000',
            '-probesize', '5000000',

            # --- Video Input (Raw) ---
            '-f', video_codec, '-use_wallclock_as_timestamps', '1', '-i', self.pipe_video,

            # --- Audio Input (PCM s16le) ---
            '-f', 's16le', '-ar', '16000', '-ac', '1', '-i', self.pipe_audio,

            '-map', '0:v', '-map', '1:a',

            # --- Video Output: H.264 Re-encode ---
            '-c:v', 'libx264',
            '-preset', 'ultrafast',
            '-tune', 'zerolatency',
            '-g', '50',
            '-r', '25',
            '-bf', '0',
            '-profile:v', 'main',

            # --- Audio Output: AAC ---
            '-af', 'aresample=async=1000',
            '-c:a', 'aac', '-ar', '16000', '-b:a', '32k',

            '-f', 'rtsp', '-rtsp_transport', 'tcp', '-max_muxing_queue_size', '400',
            self.rtsp_url,
        ]

        logger.info(f"Starting FFmpeg (V9.2 Unfiltered) for {self.camera_id}...")
        self.process = subprocess.Popen(ffmpeg_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        threading.Thread(target=self._monitor_ffmpeg, daemon=True).start()

    def _monitor_ffmpeg(self):
        if not self.process: return
        for line in self.process.stderr:
            l = line.decode(errors='ignore').strip()
            # 过滤正常进度日志
            if "frame=" not in l:
                logger.info(f"[FFmpeg {self.camera_id}] {l}")

    def stop(self):
        if self.video_writer: self.video_writer.close()
        if self.audio_writer: self.audio_writer.close()
        if self.process:
            logger.info(f"Stopping FFmpeg for {self.camera_id}...")
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except:
                self.process.kill()
            self.process = None

    # [核心修正] 移除所有 frame_type 判断，只保留基本的 seq 过滤
    # 这样可以确保 VPS/SPS/PPS 参数包顺利进入 FFmpeg
    def push_video(self, data: bytes, seq: int, is_i_frame: bool = False):
        # 允许首帧，允许大跳变，拦截小范围乱序
        if self._last_video_seq != -1 and seq <= self._last_video_seq:
            if self._last_video_seq - seq < 10000: return

        self._last_video_seq = seq
        self.video_writer.write(data)

    def push_audio_raw(self, data: bytes):
        if self.audio_writer:
            self.audio_writer.write(data)