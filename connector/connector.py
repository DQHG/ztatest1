import asyncio
import json
import logging
import ssl
import sys
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
    LOG_FILE
)
from .utils.logging_config import setup_logging
from .utils.data_models import Resource
from .resource_manager import ResourceManager

# Configure logging
setup_logging(LOG_FILE)
logger = logging.getLogger(__name__)
logging.getLogger("aioice").setLevel(logging.WARNING)


class Connector:
    def __init__(self):
        self.pc = None  # RTCPeerConnection
        self.channel = None  # RTCDataChannel
        self.resource_manager = ResourceManager()
        self.data_queues = {}
        self.signaling = None
        self.session = None
        self.loop = asyncio.get_event_loop()


    async def run(self):
        await self.reset_connection()

    async def reset_connection(self):
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
        @self.pc.on("iceconnectionstatechange")
        async def on_iceconnectionstatechange():
            logger.info(f"ICE connection state: {self.pc.iceConnectionState}")
            if self.pc.iceConnectionState in ["failed", "disconnected"]:
                logger.error("ICE connection failed or disconnected, attempting reset")
                await self.reset_connection()

        @self.pc.on("icegatheringstatechange")
        async def on_icegatheringstatechange():
            logger.info(f"ICE gathering state: {self.pc.iceGatheringState}")

        @self.pc.on("connectionstatechange")
        async def on_connectionstatechange():
            logger.info(f"Connection state: {self.pc.connectionState}")
            if self.pc.connectionState in ["failed", "disconnected"]:
                logger.error("Connection failed or disconnected, attempting reset")
                await self.reset_connection()

        @self.pc.on("datachannel")
        def on_datachannel(channel: RTCDataChannel):
            self.channel = channel
            logger.info("DataChannel received")
            self.channel.on("open", self.on_datachannel_open)
            self.channel.on("message", self.on_message)

        # Connect to the signaling server
        await self.connect_to_signaling()

    async def connect_to_signaling(self):
        ssl_context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=CA_CERT_FILE)
        ssl_context.load_cert_chain(SSL_CERT_FILE, SSL_KEY_FILE)
        ssl_context.check_hostname = False

        self.session = ClientSession()
        try:
            self.signaling = await self.session.ws_connect(
                f"{SIGNALING_SERVER_URL}?peer_id=connector",
                ssl=ssl_context
            )
            logger.info("Connected to signaling server")

            # Receive offer and send answer
            await self.receive_signaling()
        except Exception as e:
            logger.error(f"Error connecting to signaling server: {e}")
            await asyncio.sleep(5)
            await self.reset_connection()
        finally:
            await self.session.close()

    async def receive_signaling(self):
        async for msg in self.signaling:
            if msg.type == WSMsgType.TEXT:
                data = json.loads(msg.data)
                if data.get('sdp') and data.get('type') == 'offer':
                    offer = RTCSessionDescription(sdp=data['sdp'], type=data['type'])
                    await self.pc.setRemoteDescription(offer)
                    logger.info("Received offer from client")

                    # Create and send answer
                    answer = await self.pc.createAnswer()
                    await self.pc.setLocalDescription(answer)
                    await self.signaling.send_json({
                        'sdp': self.pc.localDescription.sdp,
                        'type': self.pc.localDescription.type,
                        'target_id': 'client'
                    })
                    logger.info("Sent answer to client")
                elif data.get('candidate'):
                    candidate = RTCIceCandidate(
                        sdpMid=data['candidate']['sdpMid'],
                        sdpMLineIndex=data['candidate']['sdpMLineIndex'],
                        candidate=data['candidate']['candidate']
                    )
                    await self.pc.addIceCandidate(candidate)
                    logger.info("Added ICE candidate from client")
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

    async def on_message(self, message):
        request = json.loads(message)
        action = request.get('action')
        if action == 'data':
            session_id = request.get('session_id')
            data_hex = request.get('data')
            data = bytes.fromhex(data_hex)
            if session_id not in self.data_queues:
                self.data_queues[session_id] = asyncio.Queue()
            await self.data_queues[session_id].put(data)
        elif action == 'close':
            session_id = request.get('session_id')
            if session_id in self.data_queues:
                await self.data_queues[session_id].put(None)
        elif action == 'list_resources':
            resources = self.resource_manager.list_resources()
            response = {'action': 'list_resources', 'resources': [res.dict() for res in resources]}
            self.channel.send(json.dumps(response))
        elif action == 'add_resource':
            resource_data = request.get('resource')
            success, message = self.resource_manager.add_resource_from_dict(resource_data)
            response = {'action': 'add_resource', 'success': success, 'message': message}
            self.channel.send(json.dumps(response))
            # Thông báo cho Client cập nhật danh sách tài nguyên
            await self.notify_resource_update()
        elif action == 'delete_resource':
            resource_id = request.get('resource_id')
            success, message = self.resource_manager.delete_resource_by_id(resource_id)
            response = {'action': 'delete_resource', 'success': success, 'message': message}
            self.channel.send(json.dumps(response))
            # Thông báo cho Client cập nhật danh sách tài nguyên
            await self.notify_resource_update()
        elif action == 'proxy_connect':
            session_id = request.get('session_id')
            resource_domain = request.get('resource_domain')
            resource = self.resource_manager.get_resource_by_domain(resource_domain)
            if resource:
                asyncio.create_task(self.handle_proxy_connect(session_id, resource))
            else:
                response = {'action': 'error', 'message': 'Resource not found'}
                self.channel.send(json.dumps(response))
        else:
            response = {'action': 'error', 'message': 'Unknown action'}
            self.channel.send(json.dumps(response))

    async def notify_resource_update(self):
        # Gửi thông báo cập nhật danh sách tài nguyên cho Client
        resources = self.resource_manager.list_resources()
        response = {'action': 'list_resources', 'resources': [res.dict() for res in resources]}
        self.channel.send(json.dumps(response))
    
    async def handle_proxy_connect(self, session_id, resource):
        try:
            # Connect to the actual resource
            reader, writer = await asyncio.open_connection(resource.ip, resource.port)
            logger.info(f"Connected to resource {resource.name} at {resource.ip}:{resource.port}")

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
                    logger.info(f"TCP connection closed by resource in session {session_id}")
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

    def stop(self):
        if self.pc:
            self.loop.run_until_complete(self.pc.close())
        if self.session:
            self.loop.run_until_complete(self.session.close())


if __name__ == "__main__":
        connector = Connector()
        try:
            asyncio.run(connector.run())
        except KeyboardInterrupt:
            logger.info("Connector stopped by user")
            connector.stop()
            sys.exit(0)
