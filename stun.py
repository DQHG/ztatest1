import asyncio
import logging
from aiortc import RTCIceServer, RTCPeerConnection, RTCConfiguration

# Danh sách STUN servers hợp lệ
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

# Chuyển log cho aioice thành WARNING để giảm log không cần thiết
logging.basicConfig(level=logging.INFO)
logging.getLogger("aioice").setLevel(logging.WARNING)

async def test_stun_server(stun_server):
    configuration = RTCConfiguration(iceServers=[stun_server])
    pc = RTCPeerConnection(configuration)

    # Lọc các ứng viên ICE với địa chỉ không hợp lệ
    def filter_candidate(event):
        if event.candidate:
            candidate_ip = event.candidate.split()[4]
            if candidate_ip.startswith("169.254.") or candidate_ip.startswith("127.") or candidate_ip.startswith("10."):
                logging.info(f"Filtered out invalid candidate: {candidate_ip}")
                return False
        return True

    pc.on("icecandidate", filter_candidate)

    try:
        # Tạo một DataChannel tạm thời
        channel = pc.createDataChannel("testChannel")

        # Tạo và đặt một offer để bắt đầu ICE
        await pc.setLocalDescription(await pc.createOffer())
        
        # Chờ ICE hoàn thành thu thập các ứng viên
        await asyncio.sleep(2)
        
        # Kiểm tra kết quả
        if pc.iceConnectionState == "new" and not pc.sctp:
            logging.info(f"STUN server {stun_server.urls[0]}: No candidates found (failed)")
        else:
            logging.info(f"STUN server {stun_server.urls[0]}: Success")
    except Exception as e:
        logging.error(f"STUN server {stun_server.urls[0]}: Error - {e}")
    finally:
        await pc.close()

async def main():
    tasks = [test_stun_server(server) for server in ice_servers]
    await asyncio.gather(*tasks)

if __name__ == "__main__":
    asyncio.run(main())
