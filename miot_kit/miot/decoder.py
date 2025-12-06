# -*- coding: utf-8 -*-
# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.
"""
MIoT Decoder.
"""
import asyncio
from collections import deque
import logging
import subprocess
import threading
import time
from typing import List, Callable, Coroutine, Optional
from io import BytesIO
from av.packet import Packet
from av.codec import CodecContext
from av.video.codeccontext import VideoCodecContext
from av.audio.codeccontext import AudioCodecContext
from av.audio.resampler import AudioResampler
from av.video.frame import VideoFrame
from av.audio.frame import AudioFrame
from PIL import Image

from .types import MIoTCameraFrameType, MIoTCameraCodec, MIoTCameraFrameData
from .error import MIoTMediaDecoderError

_LOGGER = logging.getLogger(__name__)


class MIoTMediaRingBuffer():
    """Ring buffer."""
    _maxlen: int
    _video_buffer: deque[MIoTCameraFrameData]
    _audio_buffer: deque[MIoTCameraFrameData]
    _cond: threading.Condition

    def __init__(self, maxlen: int = 20):
        self._maxlen = maxlen
        self._video_buffer = deque(maxlen=maxlen)
        self._audio_buffer = deque(maxlen=maxlen)
        self._cond = threading.Condition()

    def put_video(self, item: MIoTCameraFrameData) -> None:
        with self._cond:
            # When the queue is full, non-key frames are discarded first
            if len(self._video_buffer) >= self._maxlen:
                if item.frame_type == MIoTCameraFrameType.FRAME_I:
                    removed: bool = False
                    for i in range(len(self._video_buffer)):
                        if self._video_buffer[i].frame_type != MIoTCameraFrameType.FRAME_I:
                            del self._video_buffer[i]
                            removed = True
                            break
                    if not removed:
                        self._video_buffer.popleft()
                    self._video_buffer.append(item)
                    self._cond.notify()
                else:
                    # Drop non-I frame
                    pass
                _LOGGER.info("drop non-I frame, %s, %s", item.codec_id, item.timestamp)
            else:
                self._video_buffer.append(item)
                self._cond.notify()

    def put_audio(self, item: MIoTCameraFrameData) -> None:
        with self._cond:
            self._audio_buffer.append(item)
            self._cond.notify()

    def step(
            self,
            on_video_frame: Callable[[MIoTCameraFrameData], None],
            on_audio_frame: Callable[[MIoTCameraFrameData], None],
            timeout: float = 0.2
    ) -> None:
        video_data: Optional[MIoTCameraFrameData] = None
        audio_data: Optional[MIoTCameraFrameData] = None

        with self._cond:
            # 策略修改：只要有音频就优先处理音频
            # 因为音频解码极快且对延迟敏感，视频解码慢
            if self._audio_buffer:
                audio_data = self._audio_buffer.popleft()
            elif self._video_buffer:
                video_data = self._video_buffer.popleft()
            else:
                self._cond.wait(timeout=timeout)
                # 等待后再次检查
                if self._audio_buffer:
                    audio_data = self._audio_buffer.popleft()
                elif self._video_buffer:
                    video_data = self._video_buffer.popleft()

        # 在锁外执行回调
        if audio_data:
            on_audio_frame(audio_data)
        elif video_data:
            on_video_frame(video_data)

    def stop(self):
        del self._cond
        self._video_buffer.clear()
        self._audio_buffer.clear()


class MIoTMediaDecoder(threading.Thread):
    """MIoT Decoder."""
    _main_loop: asyncio.AbstractEventLoop
    _running: bool
    _frame_interval: int
    _enable_hw_accel: bool
    _enable_audio: bool

    # format: did, data, ts, channel
    _video_callback: Callable[[bytes, int, int], Coroutine]
    # format: did, data, ts, channel
    _audio_callback: Callable[[bytes, int, int], Coroutine]

    _queue: MIoTMediaRingBuffer
    _video_decoder: Optional[CodecContext]
    _audio_decoder: Optional[CodecContext]
    _resampler: AudioResampler

    _current_jpg_width: int
    _current_jpg_height: int
    _last_jpeg_ts: int

    def __init__(
        self,
        frame_interval: int,
        video_callback: Callable[[bytes, int, int], Coroutine],
        audio_callback: Optional[Callable[[bytes, int, int], Coroutine]] = None,
        enable_hw_accel: bool = False,
        enable_audio: bool = False,
        main_loop: Optional[asyncio.AbstractEventLoop] = None,
    ) -> None:
        super().__init__()
        self._main_loop = main_loop or asyncio.get_running_loop()
        self._running = False
        self._frame_interval = frame_interval
        self._enable_hw_accel = enable_hw_accel
        self._enable_audio = enable_audio

        self._video_callback = video_callback
        if enable_audio:
            if not audio_callback:
                raise MIoTMediaDecoderError("audio_callback is required when enable audio")
            else:
                self._audio_callback = audio_callback

        self._queue = MIoTMediaRingBuffer()
        self._video_decoder = None
        self._audio_decoder = None
        self._resampler = None  # type: ignore

        self._last_jpeg_ts = 0

    def run(self) -> None:
        """Start the decoder."""
        self._running = True
        while self._running:
            try:
                self._queue.step(
                    on_video_frame=self._on_video_callback,
                    on_audio_frame=self._on_audio_callback
                )
            except Exception as e:  # pylint: disable=broad-except
                _LOGGER.error("frame data handle error, %s", e)
                if self._main_loop.is_closed():
                    break
        _LOGGER.info("decoder stopped")

    def stop(self) -> None:
        """Stop the decoder."""
        self._running = False
        self._queue.stop()
        self._video_decoder = None
        self._audio_decoder = None
        self.join()

    def push_video_frame(self, frame_data: MIoTCameraFrameData) -> None:
        self._queue.put_video(frame_data)

    def push_audio_frame(self, frame_data: MIoTCameraFrameData) -> None:
        self._queue.put_audio(frame_data)

    def detect_hwaccel(self):
        try:
            result = subprocess.run(
                ["ffmpeg", "-hwaccels"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True
            )
            hw_list = result.stdout.strip().split("\n")[1:]
            return hw_list
        except FileNotFoundError:
            return []

    def choose_hw_decoder(self, codec_name, hw_methods):
        if codec_name in ("h264", "hevc"):
            if f"{codec_name}_v4l2m2m" in hw_methods:
                return f"{codec_name}_v4l2m2m"
        return codec_name

    def _on_video_callback(self, frame_data: MIoTCameraFrameData) -> None:
        if not self._video_decoder:
            # Create video decoder
            if frame_data.codec_id == MIoTCameraCodec.VIDEO_H264:
                self._video_decoder = VideoCodecContext.create("h264", "r")
            elif frame_data.codec_id == MIoTCameraCodec.VIDEO_H265:
                self._video_decoder = VideoCodecContext.create("hevc", "r")
            _LOGGER.info("video decoder created, %s", frame_data.codec_id)
        pkt = Packet(frame_data.data)
        frames: List[VideoFrame] = self._video_decoder.decode(pkt)  # type: ignore
        now_ts = int(time.time()*1000)
        if now_ts - self._last_jpeg_ts >= self._frame_interval:
            if not frames:
                _LOGGER.info("video frame is empty, %d, %d", frame_data.codec_id, frame_data.timestamp)
                self._last_jpeg_ts = now_ts
                return
            frame = frames[0]
            # _LOGGER.debug("video frame, %d, %d", frame.height, frame.width)
            rgb_frame: VideoFrame = frame.to_rgb()
            img: Image.Image = rgb_frame.to_image()
            buf: BytesIO = BytesIO()
            img.save(buf, format="JPEG", quality=90)
            jpeg_data = buf.getvalue()
            self._main_loop.call_soon_threadsafe(
                self._main_loop.create_task,
                self._video_callback(jpeg_data, frame_data.timestamp, frame_data.channel)
            )
            self._last_jpeg_ts = now_ts

    def _on_audio_callback(self, frame_data: MIoTCameraFrameData) -> None:
        if not self._audio_decoder:
            # --- [修复] 添加对 G.711A/U 的支持 ---
            codec_name = None
            if frame_data.codec_id == MIoTCameraCodec.AUDIO_OPUS:
                codec_name = "opus"
            elif frame_data.codec_id == MIoTCameraCodec.AUDIO_G711A:
                codec_name = "alaw"  # FFmpeg 中 G.711 A-law 对应的名称
            elif frame_data.codec_id == MIoTCameraCodec.AUDIO_G711U:
                codec_name = "mulaw"  # FFmpeg 中 G.711 mu-law 对应的名称

            if codec_name:
                try:
                    # 创建解码器上下文
                    self._audio_decoder = AudioCodecContext.create(codec_name, "r")
                    _LOGGER.info("Audio decoder created: %s (%s)", codec_name, frame_data.codec_id)
                except Exception as e:
                    _LOGGER.error("Failed to create audio codec %s: %s", codec_name, e)
                    return
            else:
                _LOGGER.error("Unsupported audio codec id: %s", frame_data.codec_id)
                return

            # 初始化重采样器：转为单声道 16000Hz s16 格式 (这是大多数播放器通用的格式)
            try:
                self._resampler = AudioResampler(format="s16", layout="mono", rate=16000)
            except Exception as e:
                _LOGGER.error("Failed to create resampler: %s", e)
                return

        # 开始解码
        try:
            # 必须使用 Packet 封装
            pkt = Packet(frame_data.data)

            if self._audio_decoder:
                frames: List[AudioFrame] = self._audio_decoder.decode(pkt)
                pcm_bytes: bytes = b""

                for frame in frames:
                    # 重采样
                    rs_frames = self._resampler.resample(frame)
                    for rs_frame in rs_frames:
                        pcm_bytes += rs_frame.to_ndarray().tobytes()

                # 回调发送 PCM 数据
                if pcm_bytes and self._audio_callback:
                    self._main_loop.call_soon_threadsafe(
                        self._main_loop.create_task,
                        self._audio_callback(pcm_bytes, frame_data.timestamp, frame_data.channel)
                    )
        except Exception as e:
            # 捕获解码错误，防止线程崩溃
            _LOGGER.error("Decode audio error: %s", e)


class MIoTMediaRecorder(threading.Thread):
    """MIoT Recorder."""
    _main_loop: asyncio.AbstractEventLoop
