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
    [漏桶策略] 独立线程写入管道，防止 FFmpeg 阻塞主线程
    """

    def __init__(self, pipe_path, name):
        super().__init__(daemon=True)
        self.pipe_path = pipe_path
        self.name = name
        # 300帧缓冲 (约10秒)，配合漏桶策略
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

        # [漏桶逻辑] 队列满时，清空旧数据，只留最新的，强制追赶实时
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

        self._last_video_seq = -1

    def _get_video_output_args(self):
        """
        根据环境变量获取最佳的硬件加速参数
        ENV: MILOCO_HW_ACCEL = cpu | intel | nvidia | mac | rpi
        ENV: MILOCO_HW_DEVICE = /dev/dri/renderD128 (for intel/amd)
        """
        hw_accel = os.getenv("MILOCO_HW_ACCEL", "cpu").lower()
        hw_device = os.getenv("MILOCO_HW_DEVICE", "/dev/dri/renderD128")

        # 通用参数: 固定GOP为50(2秒), 禁用B帧(低延迟), 强制25fps
        common_opts = ['-g', '50', '-bf', '0', '-r', '25', '-profile:v', 'main']

        if hw_accel in ["intel", "amd", "vaapi"]:
            logger.info(f"Using HW Accel: VAAPI ({hw_device})")
            return [
                '-vaapi_device', hw_device,
                '-vf', 'format=nv12,hwupload',  # 必须将软解的帧上传到显存
                '-c:v', 'h264_vaapi'
            ] + common_opts

        elif hw_accel in ["nvidia", "nvenc", "cuda"]:
            logger.info("Using HW Accel: NVIDIA NVENC")
            return [
                '-c:v', 'h264_nvenc',
                '-preset', 'p1',  # p1=fastest
                '-tune', 'zerolatency',  # 零延迟调优
                '-spatial-aq', '1'
            ] + common_opts

        elif hw_accel in ["mac", "apple", "videotoolbox"]:
            logger.info("Using HW Accel: Apple VideoToolbox")
            return [
                '-c:v', 'h264_videotoolbox',
                '-realtime', 'true',
                '-allow_sw', '1'
            ] + common_opts

        elif hw_accel in ["rpi", "raspberry", "v4l2m2m"]:
            logger.info("Using HW Accel: Raspberry Pi V4L2M2M")
            return [
                '-c:v', 'h264_v4l2m2m',
                '-num_capture_buffers', '16'
            ] + common_opts

        else:  # "cpu" or unknown -> Fallback to libx264
            logger.info("Using Software Encoding: libx264 (Ultrafast)")
            return [
                '-c:v', 'libx264',
                '-preset', 'ultrafast',  # 极速预设，CPU占用极低
                '-tune', 'zerolatency',  # 零延迟
            ] + common_opts

    def start(self, video_codec="hevc"):
        self.stop()
        self._last_video_seq = -1

        self.video_writer = PipeWriter(self.pipe_video, "Video")
        self.audio_writer = PipeWriter(self.pipe_audio, "Audio")
        self.video_writer.start()
        self.audio_writer.start()

        # 动态获取视频编码参数
        video_out_args = self._get_video_output_args()

        ffmpeg_cmd = [
            'ffmpeg', '-y', '-v', 'error', '-hide_banner',

            # --- Global ---
            '-use_wallclock_as_timestamps', '1',
            '-fflags', '+genpts+nobuffer+igndts',
            '-flags', 'low_delay',
            '-err_detect', 'ignore_err',
            '-analyzeduration', '5000000',
            '-probesize', '5000000',

            # --- Video Input (Raw) ---
            '-f', video_codec, '-use_wallclock_as_timestamps', '1', '-i', self.pipe_video,

            # --- Audio Input (PCM s16le from internal decoder) ---
            # 必须匹配 decoder.py 输出的 16000Hz
            '-f', 's16le', '-ar', '16000', '-ac', '1', '-i', self.pipe_audio,

            '-map', '0:v', '-map', '1:a',

            # --- Video Output (Dynamic HW/SW) ---
            *video_out_args,

            # --- Audio Output (AAC) ---
            # aresample=async=1000: 消除累积延迟的神器
            '-af', 'aresample=async=1000',
            '-c:a', 'aac', '-ar', '16000', '-b:a', '32k',

            # --- RTSP Output ---
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
            if "Error" in l or "rtsp" in l.lower():
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

    def push_video(self, data: bytes, seq: int, is_i_frame: bool = False):
        # 简单乱序过滤
        if self._last_video_seq != -1 and seq <= self._last_video_seq:
            if self._last_video_seq - seq < 10000: return
        self._last_video_seq = seq
        self.video_writer.write(data)

    def push_audio_raw(self, data: bytes):
        if self.audio_writer:
            self.audio_writer.write(data)