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
logging.getLogger("aioice").setLevel(logging.WARNING)


class Client:
    def __init__(self):
        self.pc = None
        self.channel = None
        self.dns_resolver = None
        self.protected_resources = PROTECTED_RESOURCES
        self.data_queues = {}

    async def run(self):
        # self.start_dns_server()
        await self.reset_connection()

    async def reset_connection(self):
        if self.pc:
            await self.pc.close()

        # Cấu hình STUN và TURN Server
        ice_servers = [
        RTCIceServer(urls=["stun:numb.viagenie.ca:3478"]),
        RTCIceServer(urls=["stun:iphone-stun.strato-iphone.de:3478"]),
        RTCIceServer(urls=["stun:s1.taraba.net:3478"]),
        RTCIceServer(urls=["stun:s2.taraba.net:3478"]),
        RTCIceServer(urls=["stun:stun.2talk.com:3478"]),
        RTCIceServer(urls=["stun:stun.3clogic.com:3478"]),
        RTCIceServer(urls=["stun:stun.alltel.com.au:3478"]),
        RTCIceServer(urls=["stun:stun.altar.com.pl:3478"]),
        RTCIceServer(urls=["stun:stun.avigora.com:3478"]),
        RTCIceServer(urls=["stun:stun.arbuz.ru:3478"]),
        RTCIceServer(urls=["stun:stun.a-mm.tv:3478"]),
        RTCIceServer(urls=["stun:23.21.150.121:3478"]),
        RTCIceServer(urls=["stun:stun.advfn.com:3478"]),
        RTCIceServer(urls=["stun:stun.1und1.de:3478"]),
        RTCIceServer(urls=["stun:stun.actionvoip.com:3478"]),
        RTCIceServer(urls=["stun:stun.12connect.com:3478"]),
        RTCIceServer(urls=["stun:stun.12voip.com:3478"]),
        RTCIceServer(urls=["stun:stun.2talk.co.nz:3478"]),
        RTCIceServer(urls=["stun:stun.aeta-audio.com:3478"]),
        RTCIceServer(urls=["stun:stun.acrobits.cz:3478"]),
        RTCIceServer(urls=["stun:stun.3cx.com:3478"]),
        RTCIceServer(urls=["stun:stun.aa.net.uk:3478"]),
        RTCIceServer(urls=["stun:stun.aeta.com:3478"]),
        RTCIceServer(urls=["stun:stun.antisip.com:3478"]),
        RTCIceServer(urls=["stun:stun.annatel.net:3478"])
    ]

        configuration = RTCConfiguration(iceServers=ice_servers)
        self.pc = RTCPeerConnection(configuration)

        # Đăng ký sự kiện ICE và DataChannel
        self.pc.on("iceconnectionstatechange", self.on_iceconnectionstatechange)
        self.pc.on("icecandidate", self.on_icecandidate)

        # Tạo DataChannel
        self.channel = self.pc.createDataChannel("data")
        logger.info("DataChannel created, state: %s", self.channel.readyState)

        self.channel.on("open", self.on_datachannel_open)
        self.channel.on("close", lambda: logger.info("DataChannel closed"))
        self.channel.on("error", lambda e: logger.error("DataChannel error: %s", e))
        self.channel.on("message", self.on_datachannel_message)

        # Kết nối với Signaling Server
        await self.connect_to_signaling()

    async def connect_to_signaling(self):
        ssl_context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=CA_CERT_FILE)
        ssl_context.load_cert_chain(SSL_CERT_FILE, SSL_KEY_FILE)
        ssl_context.check_hostname = False

        async with ClientSession() as session:
            async with session.ws_connect(f"{SIGNALING_SERVER_URL}?peer_id=client", ssl=ssl_context) as ws:
                logger.info("Connected to signaling server")

                # Tạo và gửi offer
                offer = await self.pc.createOffer()
                await self.pc.setLocalDescription(offer)
                await ws.send_json({
                    'sdp': self.pc.localDescription.sdp,
                    'type': self.pc.localDescription.type,
                    'target_id': 'connector'
                })
                logger.info("Offer sent to connector")

                # Nhận phản hồi từ signaling server
                async for msg in ws:
                    data = json.loads(msg.data)
                    if data.get('sdp') and data.get('type') == 'answer':
                        answer = RTCSessionDescription(sdp=data['sdp'], type=data['type'])
                        await self.pc.setRemoteDescription(answer)
                        logger.info("Received answer from connector")
                    elif data.get('candidate'):
                        await self.pc.addIceCandidate(data['candidate'])
                        logger.info("Added ICE candidate from connector")

                    # Kiểm tra trạng thái kết nối
                    if self.pc.iceConnectionState == "connected":
                        logger.info("ICE connection is established")
                        break

                asyncio.ensure_future(self.start_tcp_proxy_server())
                await self.pc.waitClosed()

    async def on_iceconnectionstatechange(self):
        logger.info(f"ICE connection state: {self.pc.iceConnectionState}")
        if self.pc.iceConnectionState == "connected":
            logger.info("ICE connection successfully established")
        elif self.pc.iceConnectionState == "failed":
            logger.error("ICE connection failed, attempting reset")
            await self.reset_connection()

    async def on_icecandidate(self, event):
        if event.candidate and not self.filter_candidate(event.candidate):
            logger.info("Filtered invalid candidate")
        else:
            logger.info("Valid ICE candidate: %s", event.candidate)

    def filter_candidate(self, candidate):
        # Loại trừ các địa chỉ không hợp lệ
        invalid_ip_ranges = ['169.254', '127.0', '10.']
        return any(candidate.candidate.startswith(ip) for ip in invalid_ip_ranges)

    async def on_datachannel_open(self):
        logger.info("DataChannel is now open")
        asyncio.ensure_future(self.interact_with_connector())

    async def on_datachannel_message(self, message):
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
            await self.handle_response(response)

    def start_dns_server(self):
        resolver = LocalDNSResolver(self.protected_resources)
        self.dns_resolver = resolver
        dns_thread = DNSResolverThread(resolver)
        dns_thread.start()
        logger.info("Local DNS server started")

    async def start_tcp_proxy_server(self):
        server = await asyncio.start_server(self.handle_tcp_connection, '0.0.0.0', 0)
        addr = server.sockets[0].getsockname()
        logger.info(f'TCP Proxy Server started on {addr}')
        async with server:
            await server.serve_forever()

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
