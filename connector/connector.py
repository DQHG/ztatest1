# connector.py

import asyncio
import json
import logging
import ssl
import socket
import threading
from aiohttp import ClientSession
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCConfiguration, RTCIceServer

from .config import (
    SIGNALING_SERVER_URL,
    SSL_CERT_FILE,
    SSL_KEY_FILE,
    CA_CERT_FILE,
    LOG_FILE
)
from .utils.logging_config import setup_logging
from .utils.data_models import Resource
from .resource_manager import ResourceManager

# Cấu hình logging
setup_logging(LOG_FILE)
logger = logging.getLogger(__name__)

class Connector:
    def __init__(self):
        self.pc = None
        self.channel = None
        self.resource_manager = ResourceManager()

    async def run(self):
        # Tạo SSL context
        ssl_context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=CA_CERT_FILE)
        ssl_context.load_cert_chain(SSL_CERT_FILE, SSL_KEY_FILE)
        # ssl_context.verify_mode = ssl.CERT_REQUIRED
        # Debugging step: Change verify_mode to CERT_OPTIONAL
        ssl_context.verify_mode = ssl.CERT_OPTIONAL
        
        # Debugging step: Disable hostname checking
        ssl_context.check_hostname = False

        # Tạo kết nối RTCPeerConnection với STUN server
        ice_servers = [RTCIceServer(urls=["stun:stun.l.google.com:19302"])]
        configuration = RTCConfiguration(iceServers=ice_servers)
        self.pc = RTCPeerConnection(configuration)

        # Kết nối đến Signaling Server
        async with ClientSession() as session:
            async with session.ws_connect(f"{SIGNALING_SERVER_URL}?peer_id=connector", ssl=ssl_context) as ws:
                logger.info("Connected to signaling server.")

                @self.pc.on("iceconnectionstatechange")
                async def on_iceconnectionstatechange():
                    logger.info(f"ICE connection state is {self.pc.iceConnectionState}")
                    if self.pc.iceConnectionState == "failed":
                        await self.pc.close()

                @self.pc.on("datachannel")
                def on_datachannel(channel):
                    self.channel = channel

                    @channel.on("message")
                    async def on_message(message):
                        # Xử lý thông điệp từ DataChannel
                        response = json.loads(message)
                        action = response.get('action')
                        if action == 'data':
                            session_id = response.get('session_id')
                            data_hex = response.get('data')
                            data = bytes.fromhex(data_hex)
                            # Đưa dữ liệu vào hàng đợi
                            if session_id not in self.data_queues:
                                self.data_queues[session_id] = asyncio.Queue()
                            await self.data_queues[session_id].put(data)
                        elif action == 'close':
                            session_id = response.get('session_id')
                            # Đưa None vào hàng đợi để kết thúc
                            if session_id in self.data_queues:
                                await self.data_queues[session_id].put(None)
                        else:
                            # Xử lý các hành động khác như trước đây
                            await self.handle_request(response)

                # Nhận offer từ Client
                async for msg in ws:
                    data = json.loads(msg.data)
                    if data.get('sdp') and data.get('type') == 'offer':
                        offer = RTCSessionDescription(sdp=data['sdp'], type=data['type'])
                        await self.pc.setRemoteDescription(offer)
                        logger.info("Received offer from client.")

                        # Tạo answer và gửi lại cho Client
                        answer = await self.pc.createAnswer()
                        await self.pc.setLocalDescription(answer)

                        await ws.send_json({
                            'sdp': self.pc.localDescription.sdp,
                            'type': self.pc.localDescription.type,
                            'target_id': 'client'
                        })
                        logger.info("Sent answer to client.")
                    elif data.get('candidate'):
                        await self.pc.addIceCandidate(data['candidate'])
                    if self.pc.iceConnectionState == "connected":
                        break

                # Giữ kết nối
                await self.pc.waitClosed()

    async def handle_request(self, request):
        action = request.get('action')
        if action == 'list_resources':
            resources = self.resource_manager.list_resources()
            response = {'action': 'list_resources', 'resources': [res.dict() for res in resources]}
            self.channel.send(json.dumps(response))
        elif action == 'proxy_connect':
            domain = request.get('domain')
            port = request.get('port', 80)
            session_id = request.get('session_id')
            await self.handle_proxy_connect(domain, port, session_id)
        else:
            response = {'action': 'error', 'message': 'Unknown action'}
            self.channel.send(json.dumps(response))

    async def handle_proxy_connect(self, domain, port, session_id):
        try:
            # Thực hiện DNS resolution nội bộ
            ip = socket.gethostbyname(domain)
            logger.info(f"Resolved {domain} to {ip}")

            # Kết nối đến tài nguyên
            reader, writer = await asyncio.open_connection(ip, port)
            logger.info(f"Connected to {domain}:{port}")

            # Tạo các task để chuyển tiếp dữ liệu
            tasks = [
                asyncio.create_task(self.datachannel_to_tcp(writer, session_id)),
                asyncio.create_task(self.tcp_to_datachannel(reader, session_id))
            ]

            await asyncio.wait(tasks)

            logger.info(f"Closing proxy_data session {session_id}")
            writer.close()
        except Exception as e:
            logger.error(f"Error in handle_proxy_connect: {e}")
            response = {'action': 'error', 'message': str(e)}
            self.channel.send(json.dumps(response))

    async def datachannel_to_tcp(self, writer, session_id):
        try:
            while True:
                # Chờ dữ liệu từ DataChannel
                data = await self.get_data_from_queue(session_id)
                if data is None:
                    break
                writer.write(data)
                await writer.drain()
        except Exception as e:
            logger.error(f"Error in datachannel_to_tcp: {e}")

    async def tcp_to_datachannel(self, reader, session_id):
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    logger.info(f"TCP connection closed by remote host in session {session_id}")
                    # Gửi thông báo kết thúc đến Client
                    message = {'action': 'close', 'session_id': session_id}
                    self.channel.send(json.dumps(message))
                    break
                # Gửi dữ liệu qua DataChannel
                message = {
                    'action': 'data',
                    'session_id': session_id,
                    'data': data.hex()
                }
                self.channel.send(json.dumps(message))
        except Exception as e:
            logger.error(f"Error in tcp_to_datachannel: {e}")

    async def get_data_from_queue(self, session_id):
        if session_id not in self.data_queues:
            self.data_queues[session_id] = asyncio.Queue()
        queue = self.data_queues[session_id]
        data = await queue.get()
        return data

if __name__ == "__main__":
    connector = Connector()
    asyncio.run(connector.run())
