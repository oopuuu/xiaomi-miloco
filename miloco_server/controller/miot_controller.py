# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
Xiaomi IoT controller
Handles Xiaomi IoT device login, authorization, and device management
"""
import logging
import os
from collections import OrderedDict
from datetime import datetime
from typing import Dict, Optional
from functools import partial
from fastapi import APIRouter, Depends, WebSocket, Query
from fastapi.responses import HTMLResponse
from fastapi.websockets import WebSocketDisconnect, WebSocketState

from miloco_server.middleware import (
    verify_token,
    verify_websocket_token
)
from miloco_server.middleware import MiotServiceException, ResourceNotFoundException
from miloco_server.schema.common_schema import NormalResponse
from miloco_server.service.manager import get_manager
from miot_kit.miot.types import MIoTCameraVideoQuality

logger = logging.getLogger(name=__name__)

router = APIRouter(prefix="/miot", tags=["Xiaomi IoT"])

manager = get_manager()


@router.get("/xiaomi_home_callback", summary="Xiaomi Home authorization callback", response_class=HTMLResponse)
async def xiaomi_home_callback(code: str, state: str):
    """Xiaomi Home authorization callback handler"""
    logger.info(
        "Xiaomi Home authorization callback: code=%s, state=%s", code, state)

    template_path = os.path.join(os.path.dirname(
        __file__), "..", "templates", "miot_login_callback.html")
    try:
        with open(template_path, "r", encoding="utf-8") as f:
            template_content = f.read()
    except FileNotFoundError as exc:
        logger.error("HTML template file not found: %s", template_path)
        raise ResourceNotFoundException("HTML template file not found") from exc

    try:
        await manager.miot_service.process_xiaomi_home_callback(code, state)
        logger.info("Xiaomi Home authorization callback processed successfully")
        title = "Authorization Successful"
        content = "Xiaomi Home authorization successful, you can close this page"
        button = "Close"
        success = True
    except MiotServiceException as e:
        logger.error(
            "Xiaomi Home authorization callback processing failed - MiOT service error: %s", e.message)
        title = "Authorization Failed"
        content = f"Xiaomi Home authorization failed: {e.message}"
        button = "Close"
        success = False
    except Exception as e:
        logger.error(
            "Unknown error occurred during Xiaomi Home authorization callback processing: %s", str(e))
        title = "Authorization Failed"
        content = f"Unknown error occurred during Xiaomi Home authorization: {str(e)}"
        button = "Close"
        success = False

    web_page = template_content.replace("TITLE_PLACEHOLDER", title)
    web_page = web_page.replace("CONTENT_PLACEHOLDER", content)
    web_page = web_page.replace("BUTTON_PLACEHOLDER", button)
    web_page = web_page.replace(
        "STATUS_PLACEHOLDER", "true" if success else "false")

    return HTMLResponse(content=web_page)


@router.get("/login_status", summary="Check MiOT login status", response_model=NormalResponse)
async def get_miot_login_status(current_user: str = Depends(verify_token)):
    """Check MiOT login status"""
    logger.info("MiOT login status API called, user: %s", current_user)
    result = await manager.miot_service.get_miot_login_status()
    logger.info("MiOT login status: Login successful")
    return NormalResponse(
        code=0,
        message="Login status checked successfully",
        data=result
    )


@router.get(path="/user_info", summary="Get MiOT user information", response_model=NormalResponse)
async def get_miot_user_info(current_user: str = Depends(verify_token)):
    """Get MiOT user information"""
    logger.info("Get MiOT user info API called, user: %s", current_user)
    user_info = await manager.miot_service.get_miot_user_info()
    logger.info("Successfully retrieved Xiaomi Home user information")
    return NormalResponse(
        code=0,
        message="MiOT user information retrieved successfully",
        data=user_info
    )


@router.get(path="/camera_list", summary="Get MiOT camera list", response_model=NormalResponse)
async def get_miot_camera_list(current_user: str = Depends(verify_token)):
    """Get MiOT camera list"""
    logger.info("Get MiOT camera list API called, user: %s", current_user)
    camera_list = await manager.miot_service.get_miot_camera_list()
    logger.info(
        "Successfully retrieved Xiaomi Home camera list - Count: %s", len(camera_list))
    return NormalResponse(
        code=0,
        message="MiOT camera list retrieved successfully",
        data=camera_list
    )


@router.get(path="/device_list", summary="Get MiOT device list", response_model=NormalResponse)
async def get_miot_device_list(current_user: str = Depends(verify_token)):
    """Get MiOT device list"""
    logger.info("get miot device list, user: %s", current_user)
    device_list = await manager.miot_service.get_miot_device_list()
    logger.info("Successfully retrieved Xiaomi Home device list - Count: %s", len(device_list))
    return NormalResponse(
        code=0,
        message="MiOT device list retrieved successfully",
        data=device_list
    )


@router.get(path="/refresh_miot_all_info", summary="Refresh MiOT all information", response_model=NormalResponse)
async def refresh_miot_all_info(current_user: str = Depends(verify_token)):
    """Refresh MiOT all information"""
    logger.info("Refresh MiOT all info API called, user: %s", current_user)
    result = await manager.miot_service.refresh_miot_all_info()
    logger.info("MiOT information refresh completed: %s", result)
    return NormalResponse(
        code=0,
        message="MiOT information refresh completed",
        data=result
    )


@router.get(path="/refresh_miot_cameras", summary="Refresh MiOT camera information", response_model=NormalResponse)
async def refresh_miot_cameras(current_user: str = Depends(verify_token)):
    """Refresh MiOT camera information"""
    logger.info("Refresh MiOT cameras API called, user: %s", current_user)
    result = await manager.miot_service.refresh_miot_cameras()
    logger.info("Successfully refreshed Xiaomi Home camera information")
    return NormalResponse(
        code=0,
        message="MiOT camera information refreshed successfully",
        data=result
    )


@router.get(path="/refresh_miot_scenes", summary="Refresh MiOT scene information", response_model=NormalResponse)
async def refresh_miot_scenes(current_user: str = Depends(verify_token)):
    """Refresh MiOT scene information"""
    logger.info("Refresh MiOT scenes API called, user: %s", current_user)
    result = await manager.miot_service.refresh_miot_scenes()
    logger.info("Successfully refreshed Xiaomi Home scene information")
    return NormalResponse(
        code=0,
        message="MiOT scene information refreshed successfully",
        data=result
    )


@router.get(path="/refresh_miot_user_info", summary="Refresh MiOT user information", response_model=NormalResponse)
async def refresh_miot_user_info(current_user: str = Depends(verify_token)):
    """Refresh MiOT user information"""
    logger.info("Refresh MiOT user info API called, user: %s", current_user)
    result = await manager.miot_service.refresh_miot_user_info()
    logger.info("Successfully refreshed Xiaomi Home user information")
    return NormalResponse(
        code=0,
        message="MiOT user information refreshed successfully",
        data=result
    )


@router.get(path="/refresh_miot_devices", summary="Refresh MiOT device information", response_model=NormalResponse)
async def refresh_miot_devices(current_user: str = Depends(verify_token)):
    """Refresh MiOT device information"""
    logger.info("Refresh MiOT devices API called, user: %s", current_user)
    result = await manager.miot_service.refresh_miot_devices()
    logger.info("Successfully refreshed Xiaomi Home device information")
    return NormalResponse(
        code=0,
        message="MiOT device information refreshed successfully",
        data=result
    )


@router.get(path="/miot_scene_actions", summary="Get MiOT scene actions list", response_model=NormalResponse)
async def get_miot_scene_actions(current_user: str = Depends(verify_token)):
    """Get MiOT scene actions list"""
    logger.info("Get MiOT actions API called, user: %s", current_user)
    actions = await manager.miot_service.get_miot_scene_actions()
    return NormalResponse(
        code=0,
        message="MiOT scene actions list retrieved successfully",
        data=actions
    )


@router.get(path="/send_notify", summary="Send notification", response_model=NormalResponse)
async def send_notify(notify: str, current_user: str = Depends(verify_token)):
    """Send notification"""
    logger.info("Send notify API called, notify: %s, user: %s", notify, current_user)
    await manager.miot_service.send_notify(notify)
    return NormalResponse(
        code=0,
        message="Notification sent successfully",
        data=None
    )


class MIoTVideoStreamManager:
    """MIoT Video WS Manager."""
    _CAMERA_CONNECT_COUNT_MAX: int = 4

    # Key: "camera_id.channel.video_quality"
    _camera_connect_map: Dict[str, Dict[str, OrderedDict[str, WebSocket]]]
    _camera_connect_id: int

    def __init__(self):
        self._camera_connect_map = {}
        self._camera_connect_id = 0
        logger.info("Init MIoT Video WS Manager")

    async def new_connection(
            self, websocket: WebSocket, user_name: str, token_hash: str, camera_id: str, channel: int,
            video_quality: int
    ) -> str:
        """New video stream connection."""

        camera_tag = f"{camera_id}.{channel}.{video_quality}"

        if camera_tag not in self._camera_connect_map or not self._camera_connect_map[camera_tag]:
            self._camera_connect_map[camera_tag] = {}

            callback_func = partial(self.__video_stream_callback, video_quality=video_quality)

            logger.info("Requesting stream start: %s", camera_tag)
            await manager.miot_service.start_video_stream(
                camera_id=camera_id,
                channel=channel,
                callback=callback_func,
                video_quality=video_quality
            )

        user_tag = f"{user_name}.{token_hash}"
        self._camera_connect_map[camera_tag].setdefault(user_tag, OrderedDict())
        connection_id = str(self._camera_connect_id)
        self._camera_connect_id += 1
        self._camera_connect_map[camera_tag][user_tag][connection_id] = websocket
        logger.info("New WS client joined group: %s (ID: %s)", camera_tag, connection_id)

        if len(self._camera_connect_map[camera_tag][user_tag]) > self._CAMERA_CONNECT_COUNT_MAX:
            logger.warning("User connection limit reached for %s, removing oldest.", camera_tag)
            _, ws = self._camera_connect_map[camera_tag][user_tag].popitem(last=False)
            try:
                if ws.client_state == WebSocketState.CONNECTED:
                    await ws.close()
            except Exception as err:
                logger.error("WebSocket close error: %s", err)
        return connection_id

    async def close_connection(
            self, user_name: str, token_hash: str, camera_id: str, channel: int, cid: str, video_quality: int
    ):
        """Close video stream connection."""
        camera_tag = f"{camera_id}.{channel}.{video_quality}"
        user_tag = f"{user_name}.{token_hash}"

        if (
                camera_tag not in self._camera_connect_map
                or user_tag not in self._camera_connect_map[camera_tag]
                or cid not in self._camera_connect_map[camera_tag][user_tag]
        ):
            return

        logger.info("Closing WS client: %s (ID: %s)", camera_tag, cid)

        try:
            ws = self._camera_connect_map[camera_tag][user_tag].pop(cid)
            if ws.client_state == WebSocketState.CONNECTED:
                await ws.close()
        except Exception as err:
            logger.error("WebSocket close error: %s", err)

        if len(self._camera_connect_map[camera_tag][user_tag]) == 0:
            self._camera_connect_map[camera_tag].pop(user_tag, None)

        # 如果该清晰度的所有用户都退出了
        if len(self._camera_connect_map[camera_tag]) == 0:
            logger.info("No clients left for %s, stopping stream.", camera_tag)
            await manager.miot_service.stop_video_stream(camera_id, channel, video_quality=video_quality)
            self._camera_connect_map.pop(camera_tag)

    async def __video_stream_callback(
            self, did: str, data: bytes, ts: int, seq: int, channel: int,
            video_quality: int, packet_type: int = 1  # <--- [新增参数] 默认为1(视频)
    ):
        """
        回调函数：负责向 WebSocket 发送数据
        packet_type: 1=视频, 2=音频
        """
        camera_tag = f"{did}.{channel}.{video_quality}"

        # [关键修改] 构建带头部的包: Type(1 byte) + Payload
        # to_bytes(1, ...) 生成 b'\x01'，to_bytes(1, ...) 生成 b'\x02'
        header = packet_type.to_bytes(1, 'big')
        packet = header + data

        # 获取该相机组下的所有连接
        if camera_tag in self._camera_connect_map:
            user_map = self._camera_connect_map[camera_tag]
            for user_tag, connections in user_map.items():
                for cid, ws in list(connections.items()):
                    try:
                        # 发送带头部的二进制包
                        await ws.send_bytes(packet)
                    except Exception as e:
                        logger.error(f"Send stream error: {e}")
                        # 可以在这里处理断开连接的逻辑


miot_video_stream_manager = MIoTVideoStreamManager()


@router.websocket("/ws/video_stream")
async def video_stream_websocket(
        websocket: WebSocket,
        camera_id: str,
        channel: int,
        video_quality: int = Query(default=MIoTCameraVideoQuality.HIGH.value),
        current_user: str = Depends(verify_websocket_token)
):
    """Video stream WebSocket Endpoint."""
    logger.info(
        "WS Handshake: User=%s, Camera=%s, CH=%d, Quality=%d",
        current_user, camera_id, channel, video_quality
    )

    start_time: datetime = datetime.now()
    token_hash: str = str(hash(websocket.cookies.get("access_token")))
    cid: Optional[str] = None

    try:
        await websocket.accept()
        cid = await miot_video_stream_manager.new_connection(
            websocket=websocket,
            user_name=current_user,
            token_hash=token_hash,
            camera_id=camera_id,
            channel=channel,
            video_quality=video_quality,
        )

        while True:
            try:
                message = await websocket.receive_text()
                if message == "ping":
                    await websocket.send_text("pong")
            except Exception:
                break

    except WebSocketDisconnect:
        logger.warning("Client disconnected: %s, Q=%d", camera_id, video_quality)
    except Exception as err:
        logger.error("WebSocket error: %s", err)
        try:
            await websocket.close(code=1011, reason=f"Server error: {str(err)}")
        except:
            pass
    finally:
        if cid:
            await miot_video_stream_manager.close_connection(
                user_name=current_user,
                token_hash=token_hash,
                camera_id=camera_id,
                channel=channel,
                cid=cid,
                video_quality=video_quality
            )

        duration = (datetime.now() - start_time).total_seconds()
        logger.info("WS Session ended. Duration: %.2fs", duration)