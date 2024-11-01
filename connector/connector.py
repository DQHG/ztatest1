# connector.py

import asyncio
import json
import logging
import ssl
import socket
from aiohttp import ClientSession
from aiortc import (
    RTCPeerConnection,
    RTCSessionDescription,
    RTCConfiguration,
    RTCIceServer,
    RTCIceCandidate
)

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
logging.getLogger("aioice").setLevel(logging.WARNING)


class Connector:
    def __init__(self):
        self.pc = None  # RTCPeerConnection object, initialized to None and set later
        self.channel = None  # Data channel for WebRTC communication, initialized to None and set later
        self.resource_manager = ResourceManager()  # Manages resources and handles resource-related actions
        self.data_queues = {}  # Dictionary to store data queues for different sessions
        self.signaling = None  # WebSocket connection to the signaling server, initialized to None and set later
        self.session = None  # aiohttp ClientSession object, initialized to None and set later

    async def run(self):
        await self.reset_connection()

    async def reset_connection(self):
        if self.pc:
            await self.pc.close()

        # Cấu hình STUN Server
        # ice_servers = [
        #     RTCIceServer(urls=["stun:stun.l.google.com:19302"]),
            
        #     RTCIceServer(urls=["stun:stun2.l.google.com:19302"]),
        #     RTCIceServer(urls=["stun:stun3.l.google.com:19302"]),
        #     RTCIceServer(urls=["stun:stun4.l.google.com:19302"]),
        # ]
        ice_servers = [
    RTCIceServer(urls=["stun:stun.relay.metered.ca:80"]),
    RTCIceServer(urls=["turn:global.relay.metered.ca:80"], username="1a711f473eae217627b45e19", credential="6eiugfMmKE1NW+Ex"),
    RTCIceServer(urls=["turn:global.relay.metered.ca:80?transport=tcp"], username="1a711f473eae217627b45e19", credential="6eiugfMmKE1NW+Ex"),
    RTCIceServer(urls=["turn:global.relay.metered.ca:443"], username="1a711f473eae217627b45e19", credential="6eiugfMmKE1NW+Ex"),
    RTCIceServer(urls=["turns:global.relay.metered.ca:443?transport=tcp"], username="1a711f473eae217627b45e19", credential="6eiugfMmKE1NW+Ex")
        ]
        configuration = RTCConfiguration(iceServers=ice_servers)
        self.pc = RTCPeerConnection(configuration)

        # Đăng ký các callback
        @self.pc.on("iceconnectionstatechange")
        async def on_iceconnectionstatechange():
            logger.info(f"ICE connection state changed to {self.pc.iceConnectionState}")
            if self.pc.iceConnectionState in ["failed", "closed"]:
                logger.warning("ICE connection failed or closed, resetting connection.")
                await self.reset_connection()

        @self.pc.on("datachannel")
        def on_datachannel(channel):
            self.channel = channel
            logger.info("Data channel established.")
            self.channel.on("message", self.on_message)

        # Kết nối đến Signaling Server
        await self.connect_to_signaling()

    async def connect_to_signaling(self):
        ssl_context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=CA_CERT_FILE)
        ssl_context.load_cert_chain(SSL_CERT_FILE, SSL_KEY_FILE)
        ssl_context.check_hostname = False

        self.session = ClientSession()  # Tạo session để kết nối signaling
        try:
            self.signaling = await self.session.ws_connect(f"{SIGNALING_SERVER_URL}?peer_id=connector", ssl=ssl_context)
            logger.info("Connected to signaling server.")

            # Nhận offer từ Client và thiết lập kết nối P2P
            await self.receive_signaling()
        finally:
            await self.cleanup()  # Đảm bảo dọn dẹp session sau khi kết nối hoàn tất

    async def receive_signaling(self):
        async for msg in self.signaling:
            data = json.loads(msg.data)
            if data.get('sdp') and data.get('type') == 'offer':
                offer = RTCSessionDescription(sdp=data['sdp'], type=data['type'])
                await self.pc.setRemoteDescription(offer)
                logger.info("Received offer from client.")

                # Tạo answer và gửi lại cho Client
                answer = await self.pc.createAnswer()
                await self.pc.setLocalDescription(answer)

                await self.signaling.send_json({
                    'sdp': self.pc.localDescription.sdp,
                    'type': self.pc.localDescription.type,
                    'target_id': 'client'
                })
                logger.info("Sent answer to client.")
            elif data.get('candidate'):
                candidate = RTCIceCandidate(
                    sdpMid=data['candidate']['sdpMid'],
                    sdpMLineIndex=data['candidate']['sdpMLineIndex'],
                    candidate=data['candidate']['candidate']
                )
                await self.pc.addIceCandidate(candidate)
                logger.info("Added ICE candidate from client")
            elif data.get('peer_info'):
                peer_info = data['peer_info']
                logger.debug(f"Received peer_info: {peer_info}")
                if 'ip' in peer_info and 'port' in peer_info:
                    await self.punch_hole(peer_info)
                else:
                    logger.error("peer_info is missing 'ip' or 'port'")
            else:
                logger.warning("Unknown message from signaling server: %s", data)

        # Giữ kết nối
        await self.pc.waitClosed()


    async def punch_hole(self, peer_info):
        # Thực hiện UDP hole punching
        peer_ip = peer_info.get('ip')
        peer_port = peer_info.get('port')
        logger.info(f"Punching hole to {peer_ip}:{peer_port}")

        transport, protocol = await asyncio.get_event_loop().create_datagram_endpoint(
            lambda: asyncio.DatagramProtocol(),
            remote_addr=(peer_ip, peer_port)
        )
        transport.sendto(b'0', (peer_ip, peer_port))
        await asyncio.sleep(1)
        transport.close()

    async def on_message(self, message):
        response = json.loads(message)
        action = response.get('action')
        if action == 'data':
            session_id = response.get('session_id')
            data_hex = response.get('data')
            data = bytes.fromhex(data_hex)
            if session_id not in self.data_queues:
                self.data_queues[session_id] = asyncio.Queue()
            asyncio.ensure_future(self.data_queues[session_id].put(data))
        elif action == 'close':
            session_id = response.get('session_id')
            if session_id in self.data_queues:
                asyncio.ensure_future(self.data_queues[session_id].put(None))
        else:
            await self.handle_request(response)

    async def handle_request(self, request):
        action = request.get('action')
        if action == 'list_resources':
            resources = self.resource_manager.list_resources()
            response = {'action': 'list_resources', 'resources': [res.dict() for res in resources]}
            self.channel.send(json.dumps(response))
        elif action == 'proxy_connect':
            session_id = request.get('session_id')
            await self.handle_proxy_connect(session_id)
        else:
            response = {'action': 'error', 'message': 'Unknown action'}
            self.channel.send(json.dumps(response))

    async def handle_proxy_connect(self, session_id):
        try:
            # Kết nối đến tài nguyên (ví dụ: ứng dụng web nội bộ)
            ip = '127.0.0.1'
            port = 5000
            reader, writer = await asyncio.open_connection(ip, port)
            logger.info(f"Connected to resource at {ip}:{port}")

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
                    message = {'action': 'close', 'session_id': session_id}
                    self.channel.send(json.dumps(message))
                    break
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

    async def cleanup(self):
        # Đảm bảo đóng session khi không cần sử dụng nữa
        if self.session:
            await self.session.close()
            logger.info("Closed aiohttp session")

if __name__ == "__main__":
    connector = Connector()
    asyncio.run(connector.run())
