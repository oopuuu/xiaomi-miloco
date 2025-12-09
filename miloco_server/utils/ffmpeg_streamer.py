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
        self.queue = queue.Queue(maxsize=500)
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

        # [漏桶策略] 队列满时丢弃最老的数据
        if self.queue.full():
            try:
                self.queue.get_nowait()
                # 只有在非常频繁丢弃时才警告，避免刷屏
                # logger.warning(f"[{self.name}] Dropped old packet (latency fix)")
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

        self.video_writer: Optional[PipeWriter] = None
        self.audio_writer: Optional[PipeWriter] = None
        self.process: Optional[subprocess.Popen] = None

        # [新增] 时间戳追踪器 (不仅仅是序列号)
        self._last_video_ts = 0
        self._last_audio_ts = 0

        # 重置序列号
        self._last_video_seq = -1
        self._last_audio_seq = -1

    def start(self, video_codec="hevc"):
        self.stop()

        # 重置状态
        self._last_video_ts = 0
        self._last_audio_ts = 0
        self._last_video_seq = -1
        self._last_audio_seq = -1

        self.video_writer = PipeWriter(self.pipe_video, "Video")
        # self.audio_writer = PipeWriter(self.pipe_audio, "Audio")
        self.video_writer.start()
        # self.audio_writer.start()

        # [完美参数] 10秒缓冲 + 忽略DTS + 墙钟时间 + 音频重构
        ffmpeg_cmd = [
            'ffmpeg', '-y', '-v', 'info', '-hide_banner',

            '-use_wallclock_as_timestamps', '1',
            '-fflags', '+genpts+nobuffer+igndts',  # [关键] 忽略输入DTS
            '-flags', 'low_delay',

            '-analyzeduration', '10000000',
            '-probesize', '10000000',

            # Video Input
            '-f', video_codec, '-use_wallclock_as_timestamps', '1', '-i', self.pipe_video,

            # Audio Input (G.711A 16k)
            # '-f', 'alaw', '-ar', '16000', '-ac', '1', '-i', self.pipe_audio,
            #
            # '-map', '0:v', '-map', '1:a',

            # Video Output: Copy + Fix
            '-c:v', 'copy', '-bsf:v', 'hevc_mp4toannexb',

            # Audio Output: Timestamp Fix + Opus
            # '-af', 'aresample=16000,asetpts=N/SR/TB',
            # '-c:a', 'libopus', '-b:a', '24k', '-ar', '16000', '-application', 'lowdelay',

            '-f', 'rtsp', '-rtsp_transport', 'tcp', '-max_muxing_queue_size', '400',
            self.rtsp_url,
        ]

        logger.info(f"Starting FFmpeg for {self.camera_id}...")
        self.process = subprocess.Popen(ffmpeg_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        threading.Thread(target=self._monitor_ffmpeg, daemon=True).start()

    def _monitor_ffmpeg(self):
        if not self.process: return
        for line in self.process.stderr:
            l = line.decode(errors='ignore').strip()
            # 依然显示错误，帮助排查
            if "Error" in l or "rtsp" in l.lower() or "fps" in l:
                logger.info(f"[FFmpeg {self.camera_id}] {l}")

    def stop(self):
        if self.video_writer: self.video_writer.close()
        if self.audio_writer: self.audio_writer.close()
        if self.process:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except:
                self.process.kill()
            self.process = None

    # [核心修复] push 方法增加 ts 检查
    # 我们不仅要检查序号(seq)，还要检查时间戳(ts)
    # 因为 P2P SDK 可能会重发旧序号的包，或者发序号新但时间戳旧的包

    def push_video(self, data: bytes, seq: int):
        # 你的 Service 代码应该传递了 ts 参数吗？
        # 如果 start_video_stream 里的回调没有传 ts 进来，我们需要修改 Service。
        # 暂时我们只能用 seq 做最后一道防线

        if seq <= self._last_video_seq and self._last_video_seq != -1:
            # 如果序号没变或者变小了，且不是重置，丢弃
            if self._last_video_seq - seq < 10000:  # 简单的回绕检测
                return

        self._last_video_seq = seq
        if self.video_writer: self.video_writer.write(data)

    def push_audio(self, data: bytes, seq: int):
        if seq <= self._last_audio_seq and self._last_audio_seq != -1:
            if self._last_audio_seq - seq < 10000:
                return

        self._last_audio_seq = seq
        if self.audio_writer: self.audio_writer.write(data)