import os
import logging
import subprocess
import threading
import queue
import time
import heapq
import re
from typing import Optional, List, Tuple
from miloco_server.config import RTSP_PORT

logger = logging.getLogger(__name__)


class VideoJitterBuffer:
    """
    视频抖动缓冲区：解决画面闪回/回退/花屏
    """

    def __init__(self, max_latency_frames=25):
        self.buffer: List[Tuple[int, bytes]] = []
        self.next_seq = -1
        self.max_latency = max_latency_frames
        self.force_reset_threshold = 1000

    def push(self, data: bytes, seq: int) -> List[bytes]:
        output_frames = []
        if self.next_seq == -1: self.next_seq = seq

        if abs(seq - self.next_seq) > self.force_reset_threshold:
            # logger.warning(f"Seq jump {self.next_seq}->{seq}")
            self.buffer = []
            self.next_seq = seq

        if seq < self.next_seq: return []

        heapq.heappush(self.buffer, (seq, data))

        while self.buffer and self.buffer[0][0] == self.next_seq:
            s, d = heapq.heappop(self.buffer)
            output_frames.append(d)
            self.next_seq += 1

        if len(self.buffer) > self.max_latency:
            new_seq, d = heapq.heappop(self.buffer)
            output_frames.append(d)
            self.next_seq = new_seq + 1
            while self.buffer and self.buffer[0][0] == self.next_seq:
                s, d = heapq.heappop(self.buffer)
                output_frames.append(d)
                self.next_seq += 1

        return output_frames


class PipeWriter(threading.Thread):
    def __init__(self, pipe_path, name, max_size=300):
        super().__init__(daemon=True)
        self.pipe_path = pipe_path
        self.name = name
        self.queue = queue.Queue(maxsize=max_size)
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

        # 简单丢包策略：如果满了，清空旧的，保证实时性
        if self.queue.full():
            try:
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
        self.video_writer: Optional[PipeWriter] = None
        self.audio_writer: Optional[PipeWriter] = None
        self.process: Optional[subprocess.Popen] = None

        # 视频 Jitter Buffer
        self.jitter_buffer = VideoJitterBuffer(max_latency_frames=30)

        # 诊断
        self._last_health_check = time.time()

    def _get_video_output_args(self):
        hw_accel = os.getenv("MILOCO_HW_ACCEL", "cpu").lower()
        hw_device = os.getenv("MILOCO_HW_DEVICE", "/dev/dri/renderD128")

        # 关键参数：FPS 模式设为 VFR (可变帧率)，适应 JitterBuffer 的输出抖动
        common_opts = ['-g', '50', '-bf', '0', '-fps_mode', 'vfr']

        if hw_accel in ["intel", "amd", "vaapi"]:
            logger.info(f"Using HW Accel: VAAPI ({hw_device})")
            return ['-vaapi_device', hw_device, '-vf', 'format=nv12,hwupload,scale_vaapi=format=nv12', '-c:v',
                    'h264_vaapi'] + common_opts
        elif hw_accel in ["nvidia", "nvenc", "cuda"]:
            logger.info("Using HW Accel: NVIDIA NVENC")
            return ['-c:v', 'h264_nvenc', '-preset', 'p1', '-tune', 'zerolatency'] + common_opts
        elif hw_accel in ["mac", "apple", "videotoolbox"]:
            logger.info("Using HW Accel: Apple VideoToolbox")
            return ['-c:v', 'h264_videotoolbox', '-realtime', 'true'] + common_opts
        elif hw_accel in ["rpi", "raspberry"]:
            logger.info("Using HW Accel: RPi")
            return ['-c:v', 'h264_v4l2m2m'] + common_opts
        else:
            logger.info("Using SW Encoding: libx264")
            return ['-c:v', 'libx264', '-preset', 'ultrafast', '-tune', 'zerolatency'] + common_opts

    def start(self, video_codec="hevc"):
        self.stop()
        self.jitter_buffer = VideoJitterBuffer(max_latency_frames=30)

        self.video_writer = PipeWriter(self.pipe_video, "Video")

        # 【修正 1】音频管道缓冲设置得非常小 (20)，防止音频在管道里积压导致滞后
        self.audio_writer = PipeWriter(self.pipe_audio, "Audio", max_size=20)

        self.video_writer.start()
        self.audio_writer.start()

        video_out_args = self._get_video_output_args()

        ffmpeg_cmd = [
            'ffmpeg', '-y', '-v', 'info', '-hide_banner',

            # --- 全局参数 ---
            # 【修正 2】彻底移除 wallclock，回归标准时间戳，解决无声问题
            '-fflags', '+genpts+nobuffer+igndts',
            '-flags', 'low_delay',
            '-analyzeduration', '1000000',
            '-probesize', '1000000',

            # --- Video Input ---
            '-f', video_codec,
            # 指定输入帧率，帮助 FFmpeg 均匀打标
            '-r', '25',
            '-i', self.pipe_video,

            # --- Audio Input ---
            '-f', 's16le', '-ar', '16000', '-ac', '1',
            # 移除 wallclock，避免时间戳冲突
            '-i', self.pipe_audio,

            '-map', '0:v', '-map', '1:a',

            # --- Video Output ---
            *video_out_args,

            # --- Audio Output ---
            # 【修正 3】同步策略微调
            # async=1: 允许音频重采样以匹配视频时钟
            # first_pts=0: 强制从 0 开始
            '-af', 'aresample=async=1:first_pts=0',
            '-c:a', 'aac', '-ar', '16000', '-b:a', '32k',

            # --- RTSP Output ---
            '-f', 'rtsp', '-rtsp_transport', 'tcp', '-max_muxing_queue_size', '400',
            self.rtsp_url,
        ]

        logger.info(f"Starting FFmpeg (Fix Audio) for {self.camera_id}...")
        self.process = subprocess.Popen(ffmpeg_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        threading.Thread(target=self._monitor_ffmpeg, daemon=True).start()

    def _monitor_ffmpeg(self):
        if not self.process: return
        pattern = re.compile(r'frame=\s*(\d+).*fps=\s*([\d.]+).*speed=\s*([\d.]+)x')
        for line in self.process.stderr:
            l = line.decode(errors='ignore').strip()
            if "frame=" in l:
                now = time.time()
                match = pattern.search(l)
                if match and (now - self._last_health_check > 30):
                    logger.info(f"[FFmpeg Status] {l}")
                    self._last_health_check = now
            elif "Error" in l or "fail" in l.lower():
                if "past duration" not in l: logger.warning(f"[FFmpeg Warning] {l}")

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

    def push_video(self, data: bytes, seq: int, is_i_frame: bool = False):
        ordered_frames = self.jitter_buffer.push(data, seq)
        for frame_data in ordered_frames:
            self.video_writer.write(frame_data)

    def push_audio_raw(self, data: bytes):
        if self.audio_writer:
            self.audio_writer.write(data)