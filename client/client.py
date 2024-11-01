import asyncio
import json
import logging
import ssl
import sys
import uuid
from aiohttp import ClientSession, WSMsgType
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
    TCP_PROXY_PORT,
    CGNAT_NETWORK
)
from .utils.logging_config import setup_logging
from .dns_resolver import LocalDNSResolver, DNSResolverThread
from .utils.data_models import Resource

# Configure logging
setup_logging(LOG_FILE)
logger = logging.getLogger(__name__)
logging.getLogger("aioice").setLevel(logging.WARNING)


class Client:
    def __init__(self):
        self.pc = None  # RTCPeerConnection
        self.channel = None  # RTCDataChannel
        self.signaling = None  # WebSocket connection
        self.session = None  # aiohttp ClientSession
        self.dns_resolver = None
        self.protected_resources = []
        self.resource_ip_map = {}
        self.data_queues = {}

    async def run(self):
        # Start DNS server
        self.start_dns_server()

        # Attempt initial connection setup
        await self.reset_connection()

        # Start TCP proxy server for forwarding data
        await self.start_tcp_proxy_server()

    async def reset_connection(self):
        retry_count = 0
        max_retries = 5
        while retry_count < max_retries:
            try:
                # Clean up any previous peer connection
                if self.pc:
                    await self.pc.close()

                # Configure ICE servers
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

                # Connect to signaling server
                await self.connect_to_signaling()
                return  # Successful connection

            except Exception as e:
                retry_count += 1
                logger.error(f"Connection attempt {retry_count}/{max_retries} failed: {e}")
                await asyncio.sleep(2 ** retry_count)

        logger.critical("Unable to connect after multiple attempts. Exiting.")
        sys.exit(1)

    async def on_iceconnectionstatechange(self):
        logger.info(f"ICE connection state: {self.pc.iceConnectionState}")
        if self.pc.iceConnectionState == "closed":
            logger.error("ICE connection closed, resetting connection")
            await self.reset_connection()

    def on_datachannel(self, channel: RTCDataChannel):
        self.channel = channel
        logger.info("DataChannel received")
        self.channel.on("open", self.on_datachannel_open)
        self.channel.on("message", self.on_datachannel_message)

    def on_icecandidate(self, candidate):
        # Send ICE candidates via the signaling server if needed
        pass

    async def connect_to_signaling(self):
        # Set up SSL context
        ssl_context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=CA_CERT_FILE)
        ssl_context.load_cert_chain(SSL_CERT_FILE, SSL_KEY_FILE)
        ssl_context.check_hostname = False

        # Set up ClientSession for signaling
        self.session = ClientSession()
        try:
            self.signaling = await self.session.ws_connect(
                f"{SIGNALING_SERVER_URL}?peer_id=client",
                ssl=ssl_context
            )
            logger.info("Connected to signaling server")

            # Create and send an offer
            offer = await self.pc.createOffer()
            await self.pc.setLocalDescription(offer)
            await self.signaling.send_json({
                'sdp': self.pc.localDescription.sdp,
                'type': self.pc.localDescription.type,
                'target_id': 'connector'
            })
            logger.info("Offer sent to connector")

            # Start receiving signaling messages
            asyncio.create_task(self.receive_signaling())

        except Exception as e:
            logger.error(f"Error connecting to signaling server: {e}")
            await self.session.close()  # Ensure session is closed on error

    async def receive_signaling(self):
        try:
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
                    elif data.get('action') == 'update_resources':
                        resources = data.get('resources', [])
                        self.update_protected_resources(resources)
                        logger.info("Updated protected resources")
                    else:
                        logger.warning(f"Unknown message from signaling server: {data}")
                elif msg.type == WSMsgType.ERROR:
                    logger.error(f"Signaling error: {msg.data}")
                    break
                elif msg.type == WSMsgType.CLOSED:
                    logger.warning("Signaling connection closed")
                    break
        finally:
            await self.session.close()

    def on_datachannel_open(self):
        logger.info("DataChannel is open")
        # Request resource list upon opening DataChannel
        request = {'action': 'request_resources'}
        self.channel.send(json.dumps(request))

    def on_datachannel_message(self, message):
        response = json.loads(message)
        action = response.get('action')
        if action == 'resource_list':
            resources = response.get('resources', [])
            self.update_protected_resources(resources)
            logger.info("Received resource list from connector")
        elif action == 'data':
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
        else:
            logger.warning(f"Unknown action received: {action}")

    def update_protected_resources(self, resources):
        self.protected_resources = [Resource(**res) for res in resources]
        self.dns_resolver.update_resources(self.protected_resources)
        self.extract_resource_ip_map()

    def extract_resource_ip_map(self):
        self.resource_ip_map = self.dns_resolver.resource_ip_map
        logger.info(f"Resource IP Map: {self.resource_ip_map}")

    async def start_tcp_proxy_server(self):
        server = await asyncio.start_server(self.handle_tcp_connection, '127.0.0.1', TCP_PROXY_PORT)
        logger.info(f'TCP Proxy Server started on port {TCP_PROXY_PORT}')
        async with server:
            await server.serve_forever()

    async def handle_tcp_connection(self, reader, writer):
        dest_addr = writer.get_extra_info('sockname')
        dest_ip = dest_addr[0]

        if dest_ip in self.resource_ip_map.values():
            session_id = str(uuid.uuid4())
            resource_domain = [k for k, v in self.resource_ip_map.items() if v == dest_ip][0]
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
