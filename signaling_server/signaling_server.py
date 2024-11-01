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

    # Lưu WebSocket của peer
    peers[peer_id] = {'ws': ws, 'ip': request.remote}

    logger.info(f'Peer {peer_id} connected with IP {request.remote}.')

    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                data = json.loads(msg.data)
                target_id = data.get('target_id')
                
                # Kiểm tra nếu peer target có tồn tại
                if target_id and target_id in peers:
                    # Gửi thông tin peer_info cho client hoặc connector
                    if data.get('action') == 'ping':
                        peer_info = {
                            'ip': peers[target_id]['ip'],
                            'port': data.get('port', 5000)  # Sử dụng port mặc định nếu không có
                        }
                        data['peer_info'] = peer_info
                        logger.info(f"Sending peer_info to {peer_id}: {peer_info}")

                    # Forward message to target peer
                    await peers[target_id]['ws'].send_json(data)
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
