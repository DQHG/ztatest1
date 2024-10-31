# connector.py

import asyncio
import json
import logging
import ssl
import socket
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
        self.data_queues = {}

    async def run(self):
        # Tạo SSL context
        ssl_context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=CA_CERT_FILE)
        ssl_context.load_cert_chain(SSL_CERT_FILE, SSL_KEY_FILE)
        ssl_context.verify_mode = ssl.CERT_OPTIONAL
        ssl_context.check_hostname = False

        # Tạo kết nối RTCPeerConnection với STUN server
        ice_servers = [
            RTCIceServer(urls=["stun:stun.l.google.com:19302"]),
            RTCIceServer(urls=["stun:stun-us.3cx.com:3478"]),
            RTCIceServer(urls=["stun:stun.2talk.com:3478"]),
            RTCIceServer(urls=["stun:stun.aa.net.uk:3478"])             
        ]
        configuration = RTCConfiguration(iceServers=ice_servers)
        self.pc = RTCPeerConnection(configuration)

        # Đăng ký các callback
        self.pc.on("iceconnectionstatechange", self.on_iceconnectionstatechange)
        self.pc.on("icegatheringstatechange", self.on_icegatheringstatechange)
        self.pc.on("signalingstatechange", self.on_signalingstatechange)
        self.pc.on("datachannel", self.on_datachannel)

        # Kết nối đến Signaling Server
        async with ClientSession() as session:
            async with session.ws_connect(f"{SIGNALING_SERVER_URL}?peer_id=connector", ssl=ssl_context) as ws:
                logger.info("Connected to signaling server.")

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

    async def on_iceconnectionstatechange(self):
        logger.info(f"ICE connection state changed to {self.pc.iceConnectionState}")
        if self.pc.iceConnectionState in ["failed", "closed"]:
            logger.warning("ICE connection failed or closed, resetting connection.")
            await self.reset_connection()

    async def on_icegatheringstatechange(self):
        logger.info(f"ICE gathering state changed to {self.pc.iceGatheringState}")

    async def on_signalingstatechange(self):
        logger.info(f"Signaling state changed to {self.pc.signalingState}")

    def on_datachannel(self, channel):
        self.channel = channel
        logger.info("Data channel established.")
        channel.on("message", self.on_message)

    async def on_message(self, message):
        response = json.loads(message)
        action = response.get('action')
        if action == 'data':
            session_id = response.get('session_id')
            data_hex = response.get('data')
            data = bytes.fromhex(data_hex)
            if session_id not in self.data_queues:
                self.data_queues[session_id] = asyncio.Queue()
            await self.data_queues[session_id].put(data)
        elif action == 'close':
            session_id = response.get('session_id')
            if session_id in self.data_queues:
                await self.data_queues[session_id].put(None)
        else:
            await self.handle_request(response)

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
            ip = socket.gethostbyname(domain)
            logger.info(f"Resolved {domain} to {ip}")
            reader, writer = await asyncio.open_connection(ip, port)
            logger.info(f"Connected to {domain}:{port}")

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

    async def reset_connection(self):
        if self.pc:
            await self.pc.close()

        ice_servers = [
            RTCIceServer(urls=["stun:stun.l.google.com:19302"]),
            RTCIceServer(urls=["stun:stun-us.3cx.com:3478"]),
            RTCIceServer(urls=["stun:stun.2talk.com:3478"]),
            RTCIceServer(urls=["stun:stun.aa.net.uk:3478"])             
        ]
        configuration = RTCConfiguration(iceServers=ice_servers)
        self.pc = RTCPeerConnection(configuration)

        self.pc.on("iceconnectionstatechange", self.on_iceconnectionstatechange)
        self.pc.on("icegatheringstatechange", self.on_icegatheringstatechange)
        self.pc.on("signalingstatechange", self.on_signalingstatechange)
        self.pc.on("datachannel", self.on_datachannel)

        logger.info("RTCPeerConnection reset and ready for new connection.")

if __name__ == "__main__":
    connector = Connector()
    asyncio.run(connector.run())
