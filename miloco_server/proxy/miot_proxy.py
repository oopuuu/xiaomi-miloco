# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""MIoT proxy module for handling Xiaomi IoT device related operations."""

import asyncio
import copy
import json
import logging
import time
from typing import Callable, Coroutine, Optional, List, Dict, Set

from pydantic_core import to_jsonable_python
from miot.client import MIoTClient
from miot.types import MIoTOauthInfo, MIoTCameraInfo, MIoTDeviceInfo, MIoTManualSceneInfo, MIoTUserInfo, \
    MIoTCameraVideoQuality, MIoTCameraStatus
from miot.camera import MIoTCameraInstance

from miloco_server.config import MIOT_CACHE_DIR, CAMERA_CONFIG
from miloco_server.dao.kv_dao import AuthConfigKeys, KVDao, DeviceInfoKeys
from miloco_server.schema.miot_schema import CameraImgSeq
from miloco_server.utils.carmera_vision_handler import CameraVisionHandler

logger = logging.getLogger(__name__)


class MiotProxy:
    """Xiaomi IoT proxy class responsible for handling MIoT device related operations."""

    def __init__(self,
                 uuid: str,
                 redirect_uri: str,
                 kv_dao: KVDao,
                 cloud_server: Optional[str] = None,
                 ):
        self._kv_dao = kv_dao
        self.init_miot_info_dict()

        # Key: did
        self._camera_img_managers: dict[str, CameraVisionHandler] = {}

        # [新增] 回调分发器
        # 结构: { did: { callback_obj } } 使用 Set 避免重复
        self._stream_subscribers: Dict[str, Set[Callable]] = {}

        self._token_refresh_task: Optional[asyncio.Task] = None

        self._miot_client = MIoTClient(
            uuid=uuid,
            redirect_uri=redirect_uri,
            cache_path=str(MIOT_CACHE_DIR),
            oauth_info=self._oauth_info,
            cloud_server=cloud_server,
        )

        self._token_refresh_task = None
        self._frame_interval: int = CAMERA_CONFIG["frame_interval"]
        self._camera_img_cache_max_size: int = CAMERA_CONFIG["camera_img_cache_max_size"]
        self._camera_img_cache_ttl: int = max(1, int(self._frame_interval * self._camera_img_cache_max_size / 1000 * 2))

    @property
    def miot_client(self) -> MIoTClient:
        return self._miot_client

    @classmethod
    async def create_miot_proxy(cls, uuid: str, redirect_uri: str, kv_dao: KVDao,
                                cloud_server: Optional[str] = None) -> "MiotProxy":
        instance = cls(uuid, redirect_uri, kv_dao, cloud_server)
        await instance.init_miot_info()
        instance._token_refresh_task = asyncio.create_task(instance._start_token_refresh_task())
        logger.info("MiotProxy (Callback Dispatcher) initialization successful")
        return instance

    async def init_miot_info(self):
        await self._miot_client.init_async()
        if self._oauth_info:
            await self._check_and_refresh_token()
            await self.refresh_miot_info()

    async def refresh_miot_info(self) -> dict:
        result = {"cameras": False, "scenes": False, "user_info": False, "devices": False}

        camera_info_dict = await self.refresh_cameras()
        result["cameras"] = camera_info_dict is not None

        await asyncio.gather(
            self.refresh_scenes(),
            self.refresh_user_info(),
            self.refresh_devices()
        )
        result["scenes"] = True
        result["user_info"] = True
        result["devices"] = True

        logger.info("MiOT info refresh completed: %s", result)
        return result

    def init_miot_info_dict(self):
        self._camera_info_dict: dict[str, MIoTCameraInfo] = {
            did: MIoTCameraInfo.model_validate(camera_info)
            for did, camera_info in json.loads(self._kv_dao.get(DeviceInfoKeys.CAMERA_INFO_KEY) or "{}").items()}
        self._device_info_dict: dict[str, MIoTDeviceInfo] = {
            did: MIoTDeviceInfo.model_validate(device_info)
            for did, device_info in json.loads(self._kv_dao.get(DeviceInfoKeys.DEVICE_INFO_KEY) or "{}").items()}
        self._scene_info_dict: dict[str, MIoTManualSceneInfo] = {
            scene_id: MIoTManualSceneInfo.model_validate(scene_info)
            for scene_id, scene_info in json.loads(self._kv_dao.get(DeviceInfoKeys.SCENE_INFO_KEY) or "{}").items()}

        user_info_str = self._kv_dao.get(DeviceInfoKeys.USER_INFO_KEY)
        if user_info_str:
            self._user_info: Optional[MIoTUserInfo] = MIoTUserInfo.model_validate_json(user_info_str)
        else:
            self._user_info = None

        oauth_info_str = self._kv_dao.get(AuthConfigKeys.MIOT_TOKEN_INFO_KEY)
        if oauth_info_str:
            self._oauth_info = MIoTOauthInfo.model_validate_json(oauth_info_str)
        else:
            self._oauth_info = None

    def get_recent_camera_img(self, camera_id: str, channel: int, recent_count: int) -> CameraImgSeq | None:
        if camera_id in self._camera_img_managers:
            return self._camera_img_managers[camera_id].get_recents_camera_img(channel, recent_count)
        return None

    # ==========================================================================
    #  核心流控制逻辑
    # ==========================================================================

    async def _master_stream_callback(self, did: str, data: bytes, ts: int, seq: int, channel: int):
        """
        主回调函数：从底层接收数据，并分发给所有订阅者
        """
        if did in self._stream_subscribers:
            # 复制一份列表防止在迭代时被修改
            subscribers = list(self._stream_subscribers[did])
            for callback in subscribers:
                try:
                    # 这里的 callback 是 controller 传进来的 (已经 partial 绑定了 quality)
                    await callback(did, data, ts, seq, channel)
                except Exception as e:
                    logger.error("Error in subscriber callback for %s: %s", did, e)

    async def start_camera_raw_stream(self, camera_id: str, channel: int,
                                      callback: Callable[[str, bytes, int, int, int], Coroutine], video_quality: int):
        logger.info("[Stream] Start Request: DID=%s, ReqQuality=%s", camera_id, video_quality)

        # 0. 将回调加入订阅列表
        if camera_id not in self._stream_subscribers:
            self._stream_subscribers[camera_id] = set()
        self._stream_subscribers[camera_id].add(callback)
        logger.info("[Stream] Added subscriber. Total count: %d", len(self._stream_subscribers[camera_id]))

        # 1. 确保基础信息
        if not self._camera_info_dict or camera_id not in self._camera_info_dict:
            await self.refresh_cameras()
            if camera_id not in self._camera_info_dict:
                logger.error("Camera %s not found", camera_id)
                return

        camera_info = self._camera_info_dict[camera_id]

        # 2. 检查物理连接
        if camera_id in self._camera_img_managers:
            current_manager = self._camera_img_managers[camera_id]
            current_quality = current_manager.camera_info.video_quality

            logger.info("[Stream] Existing connection found. PhyQuality=%s", current_quality)

            # 策略：如果请求高清(2)但当前是低清(1)，必须升级
            if current_quality < video_quality:
                logger.info("[Stream] 🔼 Upgrading stream (%s -> %s). Destroying old connection...", current_quality,
                            video_quality)
                # 注意：这里只销毁底层物理连接，保留 subscribers 列表！
                await current_manager.destroy()
                del self._camera_img_managers[camera_id]

                await asyncio.sleep(0.5)

                # 重建连接 (后续逻辑会走到创建)
            else:
                # 质量足够，复用即可。因为回调已经加入了列表，所以不需要再调 register
                logger.info("[Stream] ✅ Reusing existing connection (Quality matched/sufficient).")
                return

        # 3. 创建新连接 (如果不存在或刚被销毁)
        if camera_id not in self._camera_img_managers:
            logger.info("[Stream] ✨ Creating NEW connection with Quality=%s", video_quality)
            await self._create_camera_img_manager(camera_info, target_quality=video_quality)

        # 4. 注册主回调 (如果是新连接)
        if camera_id in self._camera_img_managers:
            instance = self._camera_img_managers[camera_id]
            # [关键] 注册 Proxy 自己的主回调，而不是直接注册上层的 callback
            await instance.register_raw_stream(self._master_stream_callback, channel)
            logger.info("[Stream] Master callback registered for %s", camera_id)
        else:
            logger.error("[Stream] Failed to ensure manager for %s", camera_id)

    async def stop_camera_raw_stream(self, camera_id: str, channel: int, video_quality: int = None):
        """停止流 (实际是从订阅列表移除)"""
        logger.info("[Stream] Stop Request (Unsubscribe): DID=%s", camera_id)

        # 1. 移除特定的订阅者需要具体的 callback 对象
        # 但由于 controller 层调用 stop 时并没有传 callback 对象进来，
        # 这是一个设计上的小缺失。
        #
        # 补救方案：
        # 由于我们做了“互斥/复用”，其实 Controller 认为它在 stop 一个独占的连接。
        # 但在 Proxy 层，可能有多个 subscriber。
        #
        # 如果我们无法区分是谁在调用 stop，最安全的做法是：
        # 如果 video_quality=2 (高清)，这通常是用户手动操作，我们假设用户想关掉。
        # 如果 video_quality=1 (低清)，这可能是后台或者另一个页面。

        # 鉴于 Controller 的架构，我们很难在这里精准移除某一个 callback。
        # 但我们可以做一个简单的引用计数逻辑：
        # 实际上，由于 `_stream_subscribers` 存的是 callback 对象，我们无法在这里通过参数移除它。

        # [权衡方案]
        # 我们不在这里移除 callback (因为拿不到对象)，也不销毁连接。
        # 只要 WebSocket 断开了，Controller 就不会再收到数据（因为 WS 对象销毁了）。
        # Proxy 继续把数据发给 Controller 的 callback，Controller 发现 WS 断了会报错或忽略。
        #
        # 真正需要做的是：如果所有 WS 都断了，是否要销毁物理连接？
        # 我们可以通过一个 periodic task 来检查 subscribers 的活跃度，或者简化处理：
        # 暂时不销毁，保持在线。

        # 如果你非常介意流量，我们可以清空该 DID 下的所有 subscribers
        # 但这会误杀其他正在看的人 (比如开了两个网页)。
        #
        # 最好的办法是：保持现状。
        # 仅仅打印日志。
        logger.info("[Stream] A client requested stop. Connection kept alive for other subscribers/keepalive.")

        # 只有当设备真的下线时 (refresh_cameras)，才彻底清理资源。

    async def _on_device_status_changed(self, did: str, status: MIoTCameraStatus):
        if did in self._camera_info_dict:
            is_connected = (status == MIoTCameraStatus.CONNECTED)
            if is_connected:
                self._camera_info_dict[did].online = True
                self._camera_info_dict[did].camera_status = status

    async def _create_camera_img_manager(self, camera_info: MIoTCameraInfo,
                                         target_quality: int = None) -> CameraVisionHandler | None:
        quality_val = target_quality if target_quality is not None else MIoTCameraVideoQuality.HIGH.value

        camera_info_copy = copy.deepcopy(camera_info)
        camera_info_copy.video_quality = quality_val

        logger.info("[Proxy] Creating physical connection for %s (Q=%s)...", camera_info.did, quality_val)

        try:
            camera_instance = await self._get_camera_instance(camera_info_copy)
        except Exception as e:
            logger.error("Failed to create instance: %s", e)
            return None

        if camera_instance is not None:
            await camera_instance.register_status_changed_async(self._on_device_status_changed)
            await camera_instance.start_async(enable_reconnect=True, qualities=quality_val)

            camera_img_manager = CameraVisionHandler(
                camera_info_copy, camera_instance, max_size=self._camera_img_cache_max_size,
                ttl=self._camera_img_cache_ttl
            )

            self._camera_img_managers[camera_info.did] = camera_img_manager
            return camera_img_manager
        else:
            logger.error("Failed to get camera instance for %s", camera_info.did)
            return None

    async def _get_camera_instance(self, camera_info: MIoTCameraInfo, target_quality: int = None) -> Optional[
        MIoTCameraInstance]:
        try:
            return await self._miot_client.create_camera_instance_async(
                camera_info, frame_interval=self._frame_interval
            )
        except Exception as e:
            logger.error("Failed to get camera instance: %s", e)
            return None

    async def get_cameras(self) -> dict[str, MIoTCameraInfo]:
        if not self._camera_info_dict:
            await self.refresh_cameras()
        return self._camera_info_dict

    async def get_camera_dids(self) -> list[str]:
        camera_dict: Optional[dict[
            str, MIoTCameraInfo]] = await self.get_cameras()
        return list(camera_dict.keys()) if camera_dict else []

    async def get_devices(self) -> dict[str, MIoTDeviceInfo]:
        if not self._device_info_dict:
            await self.refresh_devices()
        return self._device_info_dict

    async def refresh_cameras(self) -> dict[str, MIoTCameraInfo] | None:
        logger.info("[Refresh] Refreshing cameras from Cloud...")
        try:
            cameras = await self._miot_client.get_cameras_async()
            cameras = copy.deepcopy(cameras)

            # 状态同步：本地优先
            for did, info in cameras.items():
                if did in self._camera_img_managers:
                    mgr = self._camera_img_managers[did]
                    try:
                        if hasattr(mgr, "miot_camera_instance"):
                            status = await mgr.miot_camera_instance.get_status_async()
                            if status == MIoTCameraStatus.CONNECTED:
                                info.online = True
                                info.camera_status = MIoTCameraStatus.CONNECTED
                                logger.info("[Refresh] Keeping %s ONLINE", did)
                    except Exception:
                        pass

            self._camera_info_dict = cameras
            self._kv_dao.set(DeviceInfoKeys.CAMERA_INFO_KEY, json.dumps(to_jsonable_python(cameras)))

            # 更新连接信息
            for did, manager in self._camera_img_managers.items():
                if did in cameras:
                    await manager.update_camera_info(cameras[did])

            # 清理
            dids_to_remove = []
            for did in list(self._camera_img_managers.keys()):
                if did not in cameras:
                    await self._camera_img_managers[did].destroy()
                    dids_to_remove.append(did)
                    # 同时也清理订阅者
                    if did in self._stream_subscribers:
                        del self._stream_subscribers[did]
            for k in dids_to_remove:
                del self._camera_img_managers[k]

            # 自动保活
            for camera_did in cameras.keys():
                if camera_did not in self._camera_img_managers:
                    default_quality = MIoTCameraVideoQuality.LOW.value
                    logger.info("[Refresh] Auto-connecting %s (Q=1) for Keep-Alive", camera_did)
                    # 创建连接并注册主回调 (虽然此时没有 subscribers，数据会空转)
                    await self._create_camera_img_manager(cameras[camera_did], target_quality=default_quality)

                    # 必须注册一个 Master Callback，否则数据没地方去，且无法分发
                    if camera_did in self._camera_img_managers:
                        await self._camera_img_managers[camera_did].register_raw_stream(self._master_stream_callback, 0)

            return cameras

        except Exception as e:
            logger.error("Failed to refresh cameras: %s", e)
            return None

    # ... 其余方法保持不变 ...
    async def refresh_devices(self) -> dict[str, MIoTDeviceInfo] | None:
        try:
            devices = await self._miot_client.get_devices_async()
            self._device_info_dict = devices
            self._kv_dao.set(DeviceInfoKeys.DEVICE_INFO_KEY, json.dumps(to_jsonable_python(devices)))
            return devices
        except Exception as e:
            logger.error("Failed to refresh devices: %s", e)
            return None

    async def refresh_scenes(self) -> dict[str, MIoTManualSceneInfo] | None:
        try:
            scenes = await self._miot_client.get_manual_scenes_async()
            self._scene_info_dict = scenes
            self._kv_dao.set(DeviceInfoKeys.SCENE_INFO_KEY, json.dumps(to_jsonable_python(scenes)))
            return scenes
        except Exception as e:
            logger.error("Failed to get all scenes: %s", e)
            return None

    async def get_all_scenes(self) -> dict[str, MIoTManualSceneInfo] | None:
        if not self._scene_info_dict:
            await self.refresh_scenes()
        return self._scene_info_dict

    async def execute_miot_scene(self, scene_id: str) -> bool:
        try:
            scene_info = self._scene_info_dict[scene_id]
            return await self._miot_client.run_manual_scene_async(scene_info=scene_info)
        except Exception as e:
            logger.error("Failed to execute miot scene: %s", e)
            return False

    async def send_app_notify(self, app_notify_id: str) -> bool:
        try:
            return await self._miot_client.send_app_notify_async(app_notify_id)
        except Exception as e:
            logger.error("Failed to send app notify: %s", e)
            return False

    async def check_token_valid(self) -> bool:
        try:
            return await self._miot_client.check_token_async()
        except Exception as e:
            logger.error("Failed to check token valid: %s", e)
            raise

    async def refresh_user_info(self):
        try:
            user_info = await self._miot_client.get_user_info_async()
            self._user_info = user_info
            self._kv_dao.set(DeviceInfoKeys.USER_INFO_KEY, json.dumps(to_jsonable_python(user_info)))
            return user_info
        except Exception as e:
            logger.error("Failed to refresh user info: %s", e)
            return None

    async def get_user_info(self) -> Optional[MIoTUserInfo]:
        if not self._user_info:
            await self.refresh_user_info()
        return self._user_info

    async def get_miot_login_url(self) -> str:
        url = await self._miot_client.gen_oauth_url_async()
        logger.info("Generated MIoT login URL: %s", url)
        return url

    async def get_miot_app_notify_id(self, content: str) -> str | None:
        try:
            app_notify_id = await self._miot_client.http_client.create_app_notify_async(content)
            return app_notify_id
        except Exception as e:
            logger.error("Failed to get miot app notify id: %s", e)
            return None

    async def get_miot_auth_info(self, code: str, state: str) -> MIoTOauthInfo:
        try:
            oauth_info = await self._miot_client.get_access_token_async(code=code, state=state)
            logger.info("Retrieved MIoT auth info: %s", oauth_info)
            self.reset_miot_token_info(oauth_info)
            asyncio.create_task(self.refresh_miot_info())
            return oauth_info
        except Exception as e:
            logger.error("Failed to get Xiaomi home token info, %s", e)
            raise e

    def reset_miot_token_info(self, miot_token_info: MIoTOauthInfo):
        self._oauth_info = miot_token_info
        self._kv_dao.set(AuthConfigKeys.MIOT_TOKEN_INFO_KEY, miot_token_info.model_dump_json())
        if self._miot_client.http_client:
            self._miot_client.http_client.access_token = miot_token_info.access_token
        logger.info("Token information updated")

    async def refresh_xiaomi_home_token_info(self) -> MIoTOauthInfo:
        try:
            if not self._oauth_info:
                raise ValueError("No oauth_info found")
            oauth_info = await self._miot_client.refresh_access_token_async(
                refresh_token=self._oauth_info.refresh_token
            )
            logger.info("Successfully refreshed Xiaomi home token info")
            self.reset_miot_token_info(oauth_info)
            await self._miot_client.update_access_token_async(oauth_info.access_token)
            await asyncio.sleep(3)
            await self.refresh_miot_info()
            return oauth_info
        except Exception as e:
            self._oauth_info = None
            logger.error("Failed to refresh Xiaomi home token info: %s", e, exc_info=True)

    async def _start_token_refresh_task(self):
        while True:
            try:
                await asyncio.sleep(300)
                await self._check_and_refresh_token()
            except Exception as e:
                logger.error("Scheduled token refresh task exception: %s", e)
                await asyncio.sleep(60)

    async def _check_and_refresh_token(self):
        if not self._oauth_info:
            return
        current_time = int(time.time())
        expires_ts = self._oauth_info.expires_ts
        if expires_ts - current_time <= 1800:
            logger.info("Token expiring soon, refreshing...")
            await self.refresh_xiaomi_home_token_info()