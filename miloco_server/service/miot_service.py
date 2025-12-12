# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
MiOT service module
"""

import logging
from typing import List, Optional, Callable, Coroutine

from miot.types import MIoTUserInfo, MIoTCameraInfo, MIoTDeviceInfo, MIoTCameraFrameType
from miloco_server.proxy.miot_proxy import MiotProxy
from miloco_server.schema.trigger_schema import Action
from miloco_server.schema.miot_schema import CameraImgSeq, CameraInfo, DeviceInfo, SceneInfo
from miloco_server.middleware.exceptions import (
    MiotOAuthException,
    MiotServiceException,
    ValidationException,
    BusinessException,
    ResourceNotFoundException
)
from miloco_server.utils.default_action import DefaultPresetActionManager
from miloco_server.mcp.mcp_client_manager import MCPClientManager
from miloco_server.utils.ffmpeg_streamer import FFmpegStreamer

logger = logging.getLogger(__name__)


class MiotService:
    """MiOT service class"""

    def __init__(self, miot_proxy: MiotProxy, mcp_client_manager: MCPClientManager,
                 default_preset_action_manager: Optional[DefaultPresetActionManager] = None):
        self._miot_proxy = miot_proxy
        self._mcp_client_manager = mcp_client_manager
        self._default_preset_action_manager = default_preset_action_manager
        self._streamers: dict[str, FFmpegStreamer] = {}

    @property
    def miot_client(self):
        return self._miot_proxy.miot_client

    async def start_video_stream(self, camera_id: str, channel: int,
                                 callback: Callable[..., Coroutine] = None,
                                 video_quality: int = 2):
        """
        Start video stream using Hybrid Mode:
        - Video: Raw H.265 (with I-frame wait)
        - Audio: Decoded PCM (via internal decoder)
        """
        try:
            logger.info(f"Starting RTSP Service for {camera_id} (Q={video_quality})...")

            # 1. 焦土政策：销毁旧实例，确保环境纯净
            logger.info(f"Force destroying proxy for {camera_id}...")
            await self._miot_proxy.destroy_camera_proxy(camera_id)

            # 创建新实例
            await self._miot_proxy.create_camera_proxy(camera_id, target_quality=video_quality)
            camera_instance = self._miot_proxy.get_camera_instance(camera_id)
            if not camera_instance:
                raise MiotServiceException(f"Camera instance not found: {camera_id}")

            # 2. 启动 Streamer (PCM 输入模式)
            if camera_id in self._streamers:
                self._streamers[camera_id].stop()

            streamer = FFmpegStreamer(camera_id)
            streamer.start(video_codec="hevc")
            self._streamers[camera_id] = streamer

            # 3. 清理旧回调
            try:
                await camera_instance.unregister_decode_jpg_async(channel)
                await camera_instance.unregister_decode_pcm_async(channel)
                await camera_instance.unregister_raw_video_async(channel)
                await camera_instance.unregister_raw_audio_async(channel)
            except:
                pass

            # 4. 定义回调函数 (必须先定义!)

            # [视频] Raw Stream + 关键帧判断
            async def on_video_data(did, data, ts, seq, channel, frame_type=None):
                if did in self._streamers:
                    is_i = False
                    if frame_type is not None:
                        try:
                            val = frame_type.value if hasattr(frame_type, 'value') else frame_type
                            is_i = (val == 1)
                        except:
                            pass
                    # 传入 seq 和 is_i_frame 给 Streamer 做过滤
                    self._streamers[did].push_video(data, seq, is_i_frame=is_i)

                if callback:
                    try:
                        await callback(did, data, ts, seq, channel, video_quality=video_quality, packet_type=1)
                    except:
                        pass

            # [音频] Decode PCM (无 seq，直接是波形)
            # 使用我们刚才修复的 decoder.py (audioop) 来获取完美的 PCM 数据
            async def on_audio_pcm_data(did, data, ts, channel):
                if did in self._streamers:
                    # 使用 push_audio_raw 直接写入
                    self._streamers[did].push_audio_raw(data)

                # 注意：Decode PCM 回调通常不通过旧 WS 发送，因为 WS 期待的是 packet_type=2 (Raw Audio)
                # 如果前端需要声音，这里可能无法通过旧 WS 兼容，但 RTSP 是 OK 的。

            # 5. 注册回调

            # 注册视频 (Raw)
            await camera_instance.register_raw_video_async(callback=on_video_data, channel=channel, multi_reg=False)

            # 注册音频 (Decode PCM) -> 这会启动内部解码线程
            await camera_instance.register_decode_pcm_async(callback=on_audio_pcm_data, channel=channel,
                                                            multi_reg=False)

            # 6. 启动摄像头
            await camera_instance.start_async(qualities=video_quality, enable_audio=True, enable_reconnect=True)

            logger.info(f"RTSP Streamer active (Hybrid Mode): {streamer.rtsp_url}")

        except Exception as e:
            logger.error(f"Failed to start video stream: {e}", exc_info=True)
            raise MiotServiceException(f"Stream start error: {str(e)}") from e

    async def stop_video_stream(self, camera_id: str, channel: int, video_quality: int = None):
        try:
            logger.info(f"Stopping RTSP Service for {camera_id}...")
            if camera_id in self._streamers:
                self._streamers[camera_id].stop()
                del self._streamers[camera_id]
            # 必须销毁实例以停止内部解码线程
            await self._miot_proxy.destroy_camera_proxy(camera_id)
            logger.info(f"Stream stopped for {camera_id}")
        except Exception as e:
            logger.error("Failed to stop video stream: %s", e)

    # ... (其余方法保持不变) ...
    async def process_xiaomi_home_callback(self, code: str, state: str):
        try:
            logger.info("process_xiaomi_home_callback code: %s, status: %s", code, state)
            await self._miot_proxy.get_miot_auth_info(code=code, state=state)
            await self._mcp_client_manager.init_miot_mcp_clients()
        except Exception as e:
            logger.error("Failed to process Xiaomi MiOT authorization code: %s", e)
            raise MiotServiceException(f"Failed to process Xiaomi MiOT authorization code: {str(e)}") from e

    async def refresh_miot_all_info(self) -> dict:
        try:
            return await self._miot_proxy.refresh_miot_info()
        except Exception as e:
            raise MiotServiceException(f"Failed: {str(e)}") from e

    async def refresh_miot_cameras(self):
        try:
            return await self._miot_proxy.refresh_cameras()
        except Exception as e:
            raise MiotServiceException(f"Failed: {str(e)}") from e

    async def refresh_miot_scenes(self):
        try:
            return await self._miot_proxy.refresh_scenes()
        except Exception as e:
            raise MiotServiceException(f"Failed: {str(e)}") from e

    async def refresh_miot_user_info(self):
        try:
            return await self._miot_proxy.refresh_user_info()
        except Exception as e:
            raise MiotServiceException(f"Failed: {str(e)}") from e

    async def refresh_miot_devices(self):
        try:
            return await self._miot_proxy.refresh_devices()
        except Exception as e:
            raise MiotServiceException(f"Failed: {str(e)}") from e

    async def get_miot_login_status(self) -> dict:
        try:
            is_token_valid = await self._miot_proxy.check_token_valid()
            if not is_token_valid:
                login_url = await self._miot_proxy.get_miot_login_url()
                return {"is_logged_in": False, "login_url": login_url}
            return {"is_logged_in": True}
        except Exception as e:
            raise MiotOAuthException(f"Failed to check status: {str(e)}") from e

    async def get_miot_user_info(self) -> MIoTUserInfo:
        try:
            user_info = await self._miot_proxy.get_user_info()
            if not user_info: raise ResourceNotFoundException("No user info")
            return user_info
        except Exception as e:
            raise MiotServiceException(f"Failed: {str(e)}") from e

    async def get_miot_camera_list(self) -> List[CameraInfo]:
        try:
            camera_dict = await self._miot_proxy.get_cameras()
            return [CameraInfo.model_validate(info.model_dump()) for info in
                    camera_dict.values()] if camera_dict else []
        except Exception as e:
            raise MiotServiceException(f"Failed: {str(e)}") from e

    async def get_miot_device_list(self) -> List[DeviceInfo]:
        try:
            device_dict = await self._miot_proxy.get_devices()
            return [DeviceInfo.model_validate(info.model_dump()) for info in
                    device_dict.values()] if device_dict else []
        except Exception as e:
            raise MiotServiceException(f"Failed: {str(e)}") from e

    async def get_miot_cameras_img(self, camera_dids: list[str], vision_use_img_count: int) -> list[CameraImgSeq]:
        logger.info("get_miot_cameras_img, camera_dids: %s", ", ".join(camera_dids))
        try:
            all_camera_info = await self._miot_proxy.get_cameras()
            if not all_camera_info: return []
            camera_img_seqs = []
            for did, info in all_camera_info.items():
                if did in camera_dids:
                    if did not in self._streamers:
                        for channel in range(info.channel_count or 1):
                            seq = self._miot_proxy.get_recent_camera_img(did, channel, vision_use_img_count)
                            if seq: camera_img_seqs.append(seq)
            return camera_img_seqs
        except Exception as e:
            raise MiotServiceException(f"Failed: {str(e)}") from e

    async def get_miot_scene_list(self) -> List[SceneInfo]:
        try:
            scenes = await self._miot_proxy.get_all_scenes()
            return [SceneInfo(scene_id=s.scene_id, scene_name=s.scene_name) for s in scenes.values()] if scenes else []
        except Exception as e:
            raise MiotServiceException(f"Failed: {str(e)}") from e

    async def send_notify(self, notify: str) -> None:
        try:
            notify_id = await self._miot_proxy.get_miot_app_notify_id(notify)
            if not notify_id: raise ValidationException("Invalid notification")
            result = await self._miot_proxy.send_app_notify(notify_id)
            if not result: raise BusinessException("Failed to send")
        except Exception as e:
            raise BusinessException(f"Failed: {str(e)}") from e

    async def get_miot_scene_actions(self) -> List[Action]:
        try:
            if not self._default_preset_action_manager: return []
            actions = await self._default_preset_action_manager.get_miot_scene_actions()
            return list(actions.values())
        except Exception as e:
            raise MiotServiceException(f"Failed: {str(e)}") from e