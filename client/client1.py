# client.py

import aioconsole
import asyncio
import json
import logging
import ssl
import os
import sys
import platform
import uuid
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
    LOG_FILE,
    PROTECTED_RESOURCES,
    CGNAT_NETWORK
)
from .utils.logging_config import setup_logging
from .utils.data_models import Resource
from .dns_resolver import LocalDNSResolver, DNSResolverThread

# Cấu hình logging
setup_logging(LOG_FILE)
logger = logging.getLogger(__name__)
logging.getLogger("aioice").setLevel(logging.WARNING)


class Client:
    def __init__(self):
        self.pc = None
        self.channel = None
        self.dns_resolver = None
        self.protected_resources = PROTECTED_RESOURCES
        self.data_queues = {}
        self.signaling = None
        self.session = None

    async def run(self):
        await self.reset_connection()

    async def reset_connection(self):
        if self.pc:
            await self.pc.close()

        # Cấu hình STUN Server
        ice_servers = [
            RTCIceServer(urls=["stun:stun.l.google.com:19302"]),
            RTCIceServer(urls=["stun:stun1.l.google.com:19302"]),
            RTCIceServer(urls=["stun:stun2.l.google.com:19302"]),
            RTCIceServer(urls=["stun:stun3.l.google.com:19302"]),
            RTCIceServer(urls=["stun:stun4.l.google.com:19302"]),
        ]

        configuration = RTCConfiguration(iceServers=ice_servers)
        self.pc = RTCPeerConnection(configuration)

        # Đăng ký sự kiện ICE và DataChannel
        @self.pc.on("iceconnectionstatechange")
        async def on_iceconnectionstatechange():
            logger.info(f"ICE connection state: {self.pc.iceConnectionState}")
            if self.pc.iceConnectionState == "failed":
                logger.error("ICE connection failed, attempting reset")
                await self.reset_connection()

        @self.pc.on("datachannel")
        def on_datachannel(channel):
            self.channel = channel
            logger.info("DataChannel is now open")
            self.channel.on("message", self.on_datachannel_message)

        # Tạo DataChannel
        self.channel = self.pc.createDataChannel("data")
        logger.info("DataChannel created, state: %s", self.channel.readyState)

        self.channel.on("open", self.on_datachannel_open)
        self.channel.on("message", self.on_datachannel_message)

        # Kết nối với Signaling Server
        await self.connect_to_signaling()

    async def connect_to_signaling(self):
        ssl_context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=CA_CERT_FILE)
        ssl_context.load_cert_chain(SSL_CERT_FILE, SSL_KEY_FILE)
        ssl_context.check_hostname = False

        self.session = ClientSession()  # Tạo session để kết nối signaling
        try:
            self.signaling = await self.session.ws_connect(f"{SIGNALING_SERVER_URL}?peer_id=client", ssl=ssl_context)
            logger.info("Connected to signaling server")

            # Tạo và gửi offer
            offer = await self.pc.createOffer()
            await self.pc.setLocalDescription(offer)
            await self.signaling.send_json({
                'sdp': self.pc.localDescription.sdp,
                'type': self.pc.localDescription.type,
                'target_id': 'connector'
            })
            logger.info("Offer sent to connector")

            # Nhận phản hồi từ signaling server
            await self.receive_signaling()
        finally:
            await self.cleanup()  # Đảm bảo dọn dẹp session sau khi kết nối hoàn tất

    async def receive_signaling(self):
        async for msg in self.signaling:
            data = json.loads(msg.data)
            if data.get('sdp') and data.get('type') == 'answer':
                answer = RTCSessionDescription(sdp=data['sdp'], type=data['type'])
                await self.pc.setRemoteDescription(answer)
                logger.info("Received answer from connector")
            elif data.get('candidate'):
                candidate = RTCIceCandidate(
                    sdpMid=data['candidate']['sdpMid'],
                    sdpMLineIndex=data['candidate']['sdpMLineIndex'],
                    candidate=data['candidate']['candidate']
                )
                await self.pc.addIceCandidate(candidate)
                logger.info("Added ICE candidate from connector")
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

    async def cleanup(self):
        # Đóng session khi không cần sử dụng nữa
        if self.session:
            await self.session.close()
            logger.info("Closed aiohttp session")

    def on_datachannel_open(self):
        logger.info("DataChannel is open")
        asyncio.ensure_future(self.interact_with_connector())

    def on_datachannel_message(self, message):
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
            asyncio.ensure_future(self.handle_response(response))

    async def start_tcp_proxy_server(self):
        server = await asyncio.start_server(self.handle_tcp_connection, '0.0.0.0', 0)
        addr = server.sockets[0].getsockname()
        logger.info(f'TCP Proxy Server started on {addr}')
        async with server:
            await server.serve_forever()

    async def handle_tcp_connection(self, reader, writer):
        local_addr = writer.get_extra_info('sockname')
        dest_ip = local_addr[0]
        dest_port = local_addr[1]

        # Tạo một session_id để theo dõi phiên làm việc
        session_id = str(uuid.uuid4())

        # Gửi yêu cầu đến Connector
        request = {'action': 'proxy_connect', 'session_id': session_id}
        self.channel.send(json.dumps(request))

        # Chuyển tiếp dữ liệu giữa reader/writer và DataChannel
        await self.proxy_data(reader, writer, session_id)

    async def proxy_data(self, reader, writer, session_id):
        logger.info(f"Starting proxy_data session {session_id}")

        # Tạo các task để chuyển tiếp dữ liệu
        tasks = [
            asyncio.create_task(self.tcp_to_datachannel(reader, session_id)),
            asyncio.create_task(self.datachannel_to_tcp(writer, session_id))
        ]

        await asyncio.wait(tasks)

        logger.info(f"Closing proxy_data session {session_id}")
        writer.close()

    async def tcp_to_datachannel(self, reader, session_id):
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    logger.info(f"TCP connection closed by remote host in session {session_id}")
                    # Gửi thông báo kết thúc đến Connector
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

    async def get_data_from_queue(self, session_id):
        if session_id not in self.data_queues:
            self.data_queues[session_id] = asyncio.Queue()
        queue = self.data_queues[session_id]
        data = await queue.get()
        return data

    async def interact_with_connector(self):
        logger.info("Interacting with Connector")
        while True:
            print("\nAvailable actions:")
            print("1. List resources")
            print("2. Exit")

            choice = await aioconsole.ainput("Select an action: ")

            if choice == '1':
                request = {'action': 'list_resources'}
                self.channel.send(json.dumps(request))

            elif choice == '2':
                print("Exiting...")
                await self.pc.close()
                sys.exit(0)

            else:
                print("Invalid choice. Please try again.")

    async def handle_response(self, response):
        action = response.get('action')
        if action == 'list_resources':
            resources = response.get('resources', [])
            print("\nList of resources:")
            for res in resources:
                print(f"- {res['resource_id']} ({res['protocol']})")
        elif action == 'error':
            print(f"Error: {response.get('message')}")
        else:
            print(f"Unknown response: {response}")

if __name__ == "__main__":
    client = Client()
    asyncio.run(client.run())
