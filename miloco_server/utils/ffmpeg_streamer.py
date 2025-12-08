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
    """
    独立线程负责写入命名管道。
    [优化版] 实现"丢弃旧帧"策略 (Leaky Bucket)，确保零延迟。
    """
    def __init__(self, pipe_path, name):
        super().__init__(daemon=True)
        self.pipe_path = pipe_path
        self.name = name
        # [修改] 减小缓冲区大小 (1000 -> 300)
        # 300 个包大约相当于 1-2 秒的数据量，足够缓冲网络抖动，又不至于造成分钟级延迟
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
            # 使用 O_RDWR 防止 open 阻塞
            self.fd = os.open(self.pipe_path, os.O_RDWR)
            logger.info(f"[{self.name}] Pipe opened: {self.pipe_path}")
        except Exception as e:
            logger.error(f"[{self.name}] Pipe error: {e}")
            self.running = False

    def write(self, data):
        if not self.running: return
        try:
            # 尝试非阻塞放入数据
            self.queue.put_nowait(data)
        except queue.Full:
            # [核心修复] 队列满时的策略：
            # 之前是 pass (丢弃新数据)，导致队列里全是老数据 (延迟积累)。
            # 现在改为：主动取出并丢弃一个最老的数据 (get_nowait)，然后放入新数据。
            # 这保证了 FFmpeg 读到的永远是"新鲜"的。
            try:
                self.queue.get_nowait() # 弹出最老的
                self.queue.put_nowait(data) # 塞入最新的
            except:
                pass

    def run(self):
        while self.running:
            try:
                # 读出数据写入管道
                data = self.queue.get(timeout=1.0)
                if self.fd: os.write(self.fd, data)
            except queue.Empty:
                continue
            except OSError as e:
                logger.error(f"[{self.name}] Write error: {e}")
                break
            except Exception as e:
                logger.error(f"[{self.name}] Unexpected error: {e}")
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
        # [修改] 显式指定 localhost IP，防止 DNS 问题
        self.rtsp_url = rtsp_target or f"rtsp://127.0.0.1:{RTSP_PORT}/{camera_id}"

        self.pipe_video = f"/tmp/miloco_video_{camera_id}.pipe"
        self.pipe_audio = f"/tmp/miloco_audio_{camera_id}.pipe"

        self.video_writer: Optional[PipeWriter] = None
        self.audio_writer: Optional[PipeWriter] = None
        self.process: Optional[subprocess.Popen] = None

    def start(self, video_codec="hevc"):
        self.stop()  # 先清理

        self.video_writer = PipeWriter(self.pipe_video, "Video")
        self.audio_writer = PipeWriter(self.pipe_audio, "Audio")
        self.video_writer.start()
        self.audio_writer.start()

        # [优化] 增加 -tune zerolatency 和 buffer 控制
        ffmpeg_cmd = [
            'ffmpeg', '-y', '-v', 'info', '-hide_banner',

            # 全局低延迟参数
            '-use_wallclock_as_timestamps', '1',
            '-fflags', '+genpts+nobuffer',
            '-flags', 'low_delay',
            '-tune', 'zerolatency', # 显式声明零延迟调优

            # 探测缓冲保持 10秒 (足够大以获取关键帧，避免 dimensions not set)
            # 放心，有了 PipeWriter 的漏桶策略，这不会导致延迟累积
            '-analyzeduration', '10000000',
            '-probesize', '10000000',

            # Video Input
            '-f', video_codec, '-use_wallclock_as_timestamps', '1', '-i', self.pipe_video,

            # Audio Input (G.711A 16k)
            '-f', 'alaw', '-ar', '16000', '-ac', '1', '-i', self.pipe_audio,

            '-map', '0:v', '-map', '1:a',

            # Video Output
            '-c:v', 'copy', '-bsf:v', 'hevc_mp4toannexb',

            # Audio Output
            '-af', 'aresample=16000,asetpts=N/SR/TB',
            '-c:a', 'libopus', '-b:a', '24k', '-ar', '16000', '-application', 'lowdelay',

            # Output RTSP
            '-f', 'rtsp', 
            '-rtsp_transport', 'tcp',
            # 限制输出队列，防止网络拥堵时内存积压
            '-max_muxing_queue_size', '400', 
            self.rtsp_url,
        ]

        logger.info(f"Starting FFmpeg for {self.camera_id}...")
        self.process = subprocess.Popen(
            ffmpeg_cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE
        )

        # 必须启动监控线程，否则 stderr 缓冲区满了会卡死 FFmpeg
        threading.Thread(target=self._monitor_ffmpeg, daemon=True).start()

    def _monitor_ffmpeg(self):
        if not self.process: return
        for line in self.process.stderr:
            l = line.decode(errors='ignore').strip()
            # [调试] 打印所有包含 rtsp 或 Error 的日志
            if "Error" in l or "rtsp" in l.lower() or "fps" in l:
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

    def push_video(self, data):
        if self.video_writer: self.video_writer.write(data)

    def push_audio(self, data):
        if self.audio_writer: self.audio_writer.write(data)
