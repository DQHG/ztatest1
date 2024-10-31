# client.py

import asyncio
import json
import logging
import ssl
import os
import sys
import platform
import uuid
from aiohttp import ClientSession
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCConfiguration, RTCIceServer
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

class Client:
    def __init__(self):
        self.pc = None
        self.channel = None
        self.dns_resolver = None
        self.protected_resources = PROTECTED_RESOURCES
        self.data_queues = {}

    async def run(self):
        # Tạo SSL context
        ssl_context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=CA_CERT_FILE)
        ssl_context.load_cert_chain(SSL_CERT_FILE, SSL_KEY_FILE)
        ssl_context.check_hostname = False

        # Bắt đầu DNS server cục bộ
        self.start_dns_server()

        # Thêm route cho dải CGNAT
        self.add_cgnat_route()

        # Tạo kết nối RTCPeerConnection với STUN server
        ice_servers = [RTCIceServer(urls=["stun:stun.l.google.com:19302"]),
                       RTCIceServer(urls=["stun:stun-us.3cx.com:3478"]),
                       RTCIceServer(urls=["stun:stun.2talk.com:3478"]),
                       RTCIceServer(urls=["stun:stun.aa.net.uk:3478"])             
                       ]
        configuration = RTCConfiguration(iceServers=ice_servers)
        self.pc = RTCPeerConnection(configuration)

        # Kết nối đến Signaling Server
        async with ClientSession() as session:
            async with session.ws_connect(f"{SIGNALING_SERVER_URL}?peer_id=client", ssl=ssl_context) as ws:
                logger.info("Connected to signaling server.")

                @self.pc.on("iceconnectionstatechange")
                async def on_iceconnectionstatechange():
                    logger.info(f"ICE connection state is {self.pc.iceConnectionState}")
                    if self.pc.iceConnectionState == "failed":
                        logger.error("ICE connection state is failed, attempting to restart ICE")
                        try:
                            await self.pc.restartIce()  # Thử restart ICE nếu thư viện hỗ trợ
                        except Exception as e:
                            logger.error(f"Failed to restart ICE: {e}")
                    elif self.pc.iceConnectionState == "closed":
                        logger.info("ICE connection state is closed")

                # Tạo DataChannel
                self.channel = self.pc.createDataChannel("data")

                @self.channel.on("open")
                def on_open():
                    logger.info("DataChannel opened.")
                    # Bắt đầu tương tác với Connector
                    asyncio.ensure_future(self.interact_with_connector())

                @self.channel.on("message")
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
                        await self.handle_response(response)

                # Tạo offer và gửi đến Connector
                offer = await self.pc.createOffer()
                await self.pc.setLocalDescription(offer)

                await ws.send_json({
                    'sdp': self.pc.localDescription.sdp,
                    'type': self.pc.localDescription.type,
                    'target_id': 'connector'
                })
                logger.info("Sent offer to connector.")

                # Nhận answer từ Connector
                async for msg in ws:
                    data = json.loads(msg.data)
                    if data.get('sdp') and data.get('type') == 'answer':
                        answer = RTCSessionDescription(sdp=data['sdp'], type=data['type'])
                        await self.pc.setRemoteDescription(answer)
                        logger.info("Received answer from connector.")
                    elif data.get('candidate'):
                        await self.pc.addIceCandidate(data['candidate'])
                    if self.pc.iceConnectionState == "connected":
                        break

                # Khởi động TCP Proxy Server
                asyncio.ensure_future(self.start_tcp_proxy_server())

                # Giữ kết nối
                await self.pc.waitClosed()

    def start_dns_server(self):
        resolver = LocalDNSResolver(self.protected_resources)
        self.dns_resolver = resolver
        dns_thread = DNSResolverThread(resolver)
        dns_thread.start()
        logger.info("Local DNS server started.")

    def add_cgnat_route(self):
        cgnat_network = CGNAT_NETWORK
        if platform.system() == 'Linux':
            os.system(f'sudo ip route add {cgnat_network} dev lo')
        elif platform.system() == 'Darwin':  # macOS
            os.system(f'sudo route -n add {cgnat_network} -interface lo0')
        elif platform.system() == 'Windows':
            os.system(f'route add {cgnat_network} 127.0.0.1')
        logger.info("CGNAT route added.")

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

        # Tìm domain từ dest_ip
        domain = None
        for d, ip in self.dns_resolver.resource_ip_map.items():
            if ip == dest_ip:
                domain = d
                break

        if domain:
            # Tạo một session_id để theo dõi phiên làm việc
            session_id = str(uuid.uuid4())

            # Gửi yêu cầu đến Connector
            request = {'action': 'proxy_connect', 'domain': domain, 'port': dest_port, 'session_id': session_id}
            self.channel.send(json.dumps(request))

            # Chuyển tiếp dữ liệu giữa reader/writer và DataChannel
            await self.proxy_data(reader, writer, session_id)
        else:
            writer.close()

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
        while True:
            print("\nAvailable actions:")
            print("1. List resources")
            print("2. Exit")
            choice = input("Select an action: ")

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
