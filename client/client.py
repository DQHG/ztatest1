import asyncio
import json
import logging
import ssl
import sys
import uuid
import os
from aiohttp import ClientSession, WSCloseCode, WSMsgType
from aiortc import (
    RTCPeerConnection,
    RTCSessionDescription,
    RTCConfiguration,
    RTCIceServer,
    RTCIceCandidate,
    RTCDataChannel
)
from .config import (
    SIGNALING_SERVER_URL,
    SSL_CERT_FILE,
    SSL_KEY_FILE,
    CA_CERT_FILE,
    LOG_FILE,
    CGNAT_NETWORK,
    TCP_PROXY_PORT
)
from .utils.logging_config import setup_logging
from .dns_resolver import LocalDNSResolver, DNSResolverThread
from .utils.data_models import Resource

# Cấu hình logging
setup_logging(LOG_FILE)
logger = logging.getLogger(__name__)
logging.getLogger("aioice").setLevel(logging.WARNING)


class Client:
    def __init__(self):
        self.pc = None  # RTCPeerConnection
        self.channel = None  # RTCDataChannel
        self.dns_resolver = None  # LocalDNSResolver
        self.data_queues = {}
        self.signaling = None  # WebSocket connection
        self.session = None  # aiohttp ClientSession
        self.protected_resources = []  # Danh sách tài nguyên được bảo vệ
        self.protected_ips = set()
        self.resource_domain_map = {}
        self.loop = asyncio.get_event_loop()

    async def run(self):
        self.start_dns_server()
        await self.reset_connection()
        await self.start_tcp_proxy_server()

    async def reset_connection(self):
        retry_count = 0
        max_retries = 5
        while retry_count < max_retries:
            try:
                if self.pc:
                    await self.pc.close()

                # ICE server configuration
                ice_servers = [
                    RTCIceServer(urls=["stun:stun.relay.metered.ca:80"]),
                    RTCIceServer(urls=["turn:global.relay.metered.ca:80"], username="1a711f473eae217627b45e19", credential="6eiugfMmKE1NW+Ex"),
                    RTCIceServer(urls=["turn:global.relay.metered.ca:80?transport=tcp"], username="1a711f473eae217627b45e19", credential="6eiugfMmKE1NW+Ex"),
                    RTCIceServer(urls=["turn:global.relay.metered.ca:443"], username="1a711f473eae217627b45e19", credential="6eiugfMmKE1NW+Ex"),
                    RTCIceServer(urls=["turns:global.relay.metered.ca:443?transport=tcp"], username="1a711f473eae217627b45e19", credential="6eiugfMmKE1NW+Ex")
                        ]
                configuration = RTCConfiguration(iceServers=ice_servers)
                self.pc = RTCPeerConnection(configuration)

                # Register event handlers
                self.pc.on("iceconnectionstatechange", self.on_iceconnectionstatechange)
                self.pc.on("datachannel", self.on_datachannel)
                self.pc.on("icecandidate", self.on_icecandidate)

                # Create DataChannel if not already created
                if not self.channel:
                    self.channel = self.pc.createDataChannel("data")
                    logger.info("DataChannel created")
                    self.channel.on("open", self.on_datachannel_open)
                    self.channel.on("message", self.on_datachannel_message)

                # Connect to the signaling server
                await self.connect_to_signaling()
                return  # Kết nối thành công, thoát khỏi hàm
            except Exception as e:
                retry_count += 1
                logger.error(f"Kết nối thất bại ({retry_count}/{max_retries}): {e}")
                await asyncio.sleep(2 ** retry_count)
        logger.critical("Không thể kết nối sau nhiều lần thử. Thoát chương trình.")
        sys.exit(1)

    async def on_iceconnectionstatechange(self):
        logger.info(f"ICE connection state: {self.pc.iceConnectionState}")
        if self.pc.iceConnectionState == "failed":
            logger.error("ICE connection failed, attempting reset")
            await self.reset_connection()

    def on_datachannel(self, channel: RTCDataChannel):
        self.channel = channel
        logger.info("DataChannel received")
        self.channel.on("open", self.on_datachannel_open)
        self.channel.on("message", self.on_datachannel_message)

    def on_icecandidate(self, candidate):
        # Có thể gửi ICE candidate đến signaling server nếu cần
        pass

    async def connect_to_signaling(self):
        ssl_context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=CA_CERT_FILE)
        ssl_context.load_cert_chain(SSL_CERT_FILE, SSL_KEY_FILE)
        ssl_context.check_hostname = False

        self.session = ClientSession()
        self.signaling = await self.session.ws_connect(
            f"{SIGNALING_SERVER_URL}?peer_id=client",
            ssl=ssl_context
        )
        logger.info("Connected to signaling server")

        # Create and send offer
        offer = await self.pc.createOffer()
        await self.pc.setLocalDescription(offer)
        await self.signaling.send_json({
            'sdp': self.pc.localDescription.sdp,
            'type': self.pc.localDescription.type,
            'target_id': 'connector'
        })
        logger.info("Offer sent to connector")

        # Receive signaling messages
        asyncio.create_task(self.receive_signaling())

    async def receive_signaling(self):
        async for msg in self.signaling:
            if msg.type == WSMsgType.TEXT:
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
                else:
                    logger.warning(f"Unknown message from signaling server: {data}")
            elif msg.type == WSMsgType.ERROR:
                logger.error(f"Signaling error: {msg.data}")
                break
            elif msg.type == WSMsgType.CLOSED:
                logger.warning("Signaling connection closed")
                break

    def on_datachannel_open(self):
        logger.info("DataChannel is open")
        # Khởi động giao diện tương tác với người dùng
        asyncio.create_task(self.interactive_menu())

    def on_datachannel_message(self, message):
        response = json.loads(message)
        action = response.get('action')
        if action == 'data':
            session_id = response.get('session_id')
            data_hex = response.get('data')
            data = bytes.fromhex(data_hex)
            if session_id not in self.data_queues:
                self.data_queues[session_id] = asyncio.Queue()
            asyncio.create_task(self.data_queues[session_id].put(data))
        elif action == 'close':
            session_id = response.get('session_id')
            if session_id in self.data_queues:
                asyncio.create_task(self.data_queues[session_id].put(None))
        elif action == 'list_resources':
            resources = response.get('resources', [])
            self.update_protected_resources(resources)
            logger.info("Danh sách tài nguyên đã được cập nhật.")
            self.display_resources()
        elif action == 'add_resource':
            success = response.get('success')
            message = response.get('message')
            print(f"Thêm tài nguyên {'thành công' if success else 'thất bại'}: {message}")
        elif action == 'delete_resource':
            success = response.get('success')
            message = response.get('message')
            print(f"Xóa tài nguyên {'thành công' if success else 'thất bại'}: {message}")
        elif action == 'error':
            print(f"Lỗi: {response.get('message')}")
        else:
            print(f"Phản hồi không xác định: {response}")

    def update_protected_resources(self, resources):
        self.protected_resources = [Resource(**res) for res in resources]
        self.dns_resolver.update_resources(self.protected_resources)
        self.extract_protected_ips()

    def extract_protected_ips(self):
        self.protected_ips = set(self.dns_resolver.resource_ip_map.values())
        self.resource_domain_map = {
            ip: domain for domain, ip in self.dns_resolver.resource_ip_map.items()
        }
        logger.info(f"Protected IPs: {self.protected_ips}")

    async def start_tcp_proxy_server(self):
        server = await asyncio.start_server(self.handle_tcp_connection, '127.0.0.1', TCP_PROXY_PORT)
        logger.info(f'TCP Proxy Server started on port {TCP_PROXY_PORT}')
        async with server:
            await server.serve_forever()

    async def handle_tcp_connection(self, reader, writer):
        dest_addr = writer.get_extra_info('sockname')
        dest_ip = dest_addr[0]

        if dest_ip in self.protected_ips:
            session_id = str(uuid.uuid4())
            resource_domain = self.resource_domain_map[dest_ip]
            request = {
                'action': 'proxy_connect',
                'session_id': session_id,
                'resource_domain': resource_domain
            }
            self.channel.send(json.dumps(request))
            await self.proxy_data(reader, writer, session_id)
        else:
            writer.close()
            logger.warning(f"Connection to unprotected IP {dest_ip} is not allowed.")

    async def proxy_data(self, reader, writer, session_id):
        logger.info(f"Starting proxy_data session {session_id}")

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
                    logger.info(f"TCP connection closed by client in session {session_id}")
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

    async def get_data_from_queue(self, session_id):
        if session_id not in self.data_queues:
            self.data_queues[session_id] = asyncio.Queue()
        queue = self.data_queues[session_id]
        data = await queue.get()
        return data

    def start_dns_server(self):
        self.dns_resolver = LocalDNSResolver(self.protected_resources)
        dns_thread = DNSResolverThread(self.dns_resolver)
        dns_thread.start()
        logger.info("Local DNS server started.")

    def display_resources(self):
        print("\nDanh sách tài nguyên được bảo vệ:")
        for res in self.protected_resources:
            print(f"- {res.resource_id}: {res.name} ({res.protocol}) tại {res.ip}:{res.port}")

    async def interactive_menu(self):
        # Giao diện tương tác với người dùng để quản lý tài nguyên
        while True:
            print("\nChọn chức năng:")
            print("1. Xem danh sách tài nguyên")
            print("2. Thêm tài nguyên")
            print("3. Xóa tài nguyên")
            print("4. Thoát")

            choice = input("Lựa chọn của bạn: ")

            if choice == '1':
                request = {'action': 'list_resources'}
                self.channel.send(json.dumps(request))
            elif choice == '2':
                resource_data = await self.get_resource_input()
                request = {'action': 'add_resource', 'resource': resource_data}
                self.channel.send(json.dumps(request))
            elif choice == '3':
                resource_id = input("Nhập ID tài nguyên cần xóa: ")
                request = {'action': 'delete_resource', 'resource_id': resource_id}
                self.channel.send(json.dumps(request))
            elif choice == '4':
                print("Đang thoát...")
                await self.stop()
                break
            else:
                print("Lựa chọn không hợp lệ. Vui lòng thử lại.")

    async def get_resource_input(self):
        # Nhận thông tin tài nguyên từ người dùng
        resource_data = {}
        resource_data['resource_id'] = input("ID tài nguyên: ")
        resource_data['name'] = input("Tên miền: ")
        resource_data['protocol'] = input("Giao thức (HTTP/HTTPS/SSH/RDP): ")
        resource_data['description'] = input("Mô tả: ")
        resource_data['ip'] = input("Địa chỉ IP: ")
        resource_data['port'] = int(input("Cổng: "))
        resource_data['username'] = input("Tên người dùng (nếu có): ")
        return resource_data

    async def stop(self):
        if self.pc:
            await self.pc.close()
        if self.session:
            await self.session.close()
        sys.exit(0)


if __name__ == "__main__":
    client = Client()
    try:
        asyncio.run(client.run())
    except KeyboardInterrupt:
        logger.info("Client stopped by user")
        asyncio.run(client.stop())
