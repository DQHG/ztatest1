# signaling_server.py

import asyncio
import json
import logging
import ssl
from aiohttp import web, WSCloseCode

from .config import (
    SIGNALING_SERVER_HOST,
    SIGNALING_SERVER_PORT,
    SSL_CERT_FILE,
    SSL_KEY_FILE,
    CA_CERT_FILE,
    LOG_FILE
)
from .utils.logging_config import setup_logging

# Cấu hình logging
setup_logging(LOG_FILE)
logger = logging.getLogger(__name__)

peers = {}

async def websocket_handler(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    peer_id = request.query.get('peer_id')
    if not peer_id:
        await ws.close(code=WSCloseCode.PROTOCOL_ERROR, message='Missing peer_id')
        return ws

    peers[peer_id] = ws
    logger.info(f'Peer {peer_id} connected.')

    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                data = json.loads(msg.data)
                target_id = data.get('target_id')
                if target_id and target_id in peers:
                    # Forward message to target peer
                    await peers[target_id].send_json(data)
                    logger.info(f'Forwarded message from {peer_id} to {target_id}')
                else:
                    logger.warning(f'Target {target_id} not found for peer {peer_id}')
            elif msg.type == web.WSMsgType.ERROR:
                logger.error(f'Connection with peer {peer_id} closed with exception {ws.exception()}')
    finally:
        del peers[peer_id]
        logger.info(f'Peer {peer_id} disconnected.')

    return ws

app = web.Application()
app.add_routes([web.get('/ws', websocket_handler)])

if __name__ == '__main__':
    ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ssl_context.load_cert_chain(SSL_CERT_FILE, SSL_KEY_FILE)
    ssl_context.load_verify_locations(CA_CERT_FILE)
    ssl_context.verify_mode = ssl.CERT_REQUIRED

    web.run_app(app, host=SIGNALING_SERVER_HOST, port=SIGNALING_SERVER_PORT, ssl_context=ssl_context)
    # web.run_app(app, host=SIGNALING_SERVER_HOST, port=SIGNALING_SERVER_PORT)

