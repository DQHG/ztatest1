import asyncio
import json
import logging
import ssl
import sys
from aiohttp import ClientSession, WSMsgType, WSServerHandshakeError
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
from .resource_manager import ResourceManager

# Configure logging
setup_logging(LOG_FILE)
logger = logging.getLogger(__name__)
logging.getLogger("aioice").setLevel(logging.WARNING)


class Connector:
    def __init__(self, resource_manager):
        self.pc = None  # RTCPeerConnection
        self.channel = None  # RTCDataChannel
        self.resource_manager = resource_manager
        self.signaling = None
        self.session = None
        self.data_queues = {}

    async def run(self):
        await self.reset_connection()

    async def reset_connection(self):
        retry_count = 0
        max_retries = 5
        while retry_count < max_retries:
            try:
                # Clean up any previous peer connection
                if self.pc:
                    await self.pc.close()

                # ICE server configuration
                ice_servers = [
                    RTCIceServer(urls=["stun:stun.l.google.com:19302"]),
                ]
                configuration = RTCConfiguration(iceServers=ice_servers)
                self.pc = RTCPeerConnection(configuration)

                # Register event handlers
                self.pc.on("iceconnectionstatechange", self.on_iceconnectionstatechange)
                self.pc.on("datachannel", self.on_datachannel)
                self.pc.on("icecandidate", self.on_icecandidate)

                # Connect to the signaling server
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
                f"{SIGNALING_SERVER_URL}?peer_id=connector",
                ssl=ssl_context
            )
            logger.info("Connected to signaling server")

            # Start receiving signaling messages
            await self.receive_signaling()

        except WSServerHandshakeError as e:
            logger.error(f"WebSocket handshake error: {e}")
        except ssl.SSLError as e:
            logger.error(f"SSL error during WebSocket connection: {e}")
        except Exception as e:
            logger.error(f"Error connecting to signaling server: {e}")
        finally:
            if self.session:
                await self.session.close()

    async def receive_signaling(self):
        try:
            async for msg in self.signaling:
                if msg.type == WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    if data.get('sdp') and data.get('type') == 'offer':
                        if self.pc.signalingState == 'closed':
                            logger.error("Cannot handle offer in closed signaling state")
                            break
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
        except Exception as e:
            logger.error(f"Error during signaling message reception: {e}")
        finally:
            # Ensure session and WebSocket are closed
            if self.session:
                await self.session.close()

    def on_datachannel_message(self, message):
        request = json.loads(message)
        action = request.get('action')
        if action == 'request_resources':
            # Send the list of protected resources to the client
            resources = self.resource_manager.list_resources()
            response = {
                'action': 'resource_list',
                'resources': [res.dict() for res in resources]
            }
            self.channel.send(json.dumps(response))
        elif action == 'proxy_connect':
            session_id = request.get('session_id')
            resource_domain = request.get('resource_domain')
            resource = self.resource_manager.get_resource_by_domain(resource_domain)
            if resource:
                asyncio.create_task(self.handle_proxy_connect(session_id, resource))
            else:
                response = {'action': 'error', 'message': 'Resource not found'}
                self.channel.send(json.dumps(response))
        elif action == 'data':
            session_id = request.get('session_id')
            data_hex = request.get('data')
            data = bytes.fromhex(data_hex)
            if session_id not in self.data_queues:
                self.data_queues[session_id] = asyncio.Queue()
            asyncio.create_task(self.data_queues[session_id].put(data))
        elif action == 'close':
            session_id = request.get('session_id')
            if session_id in self.data_queues:
                asyncio.create_task(self.data_queues[session_id].put(None))
        else:
            logger.warning(f"Unknown action received: {action}")

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
            asyncio.run(self.pc.close())
        if self.session:
            asyncio.run(self.session.close())


if __name__ == "__main__":
    resource_manager = ResourceManager()
    connector = Connector(resource_manager)
    try:
        asyncio.run(connector.run())
    except KeyboardInterrupt:
        logger.info("Connector stopped by user")
        connector.stop()
        sys.exit(0)
