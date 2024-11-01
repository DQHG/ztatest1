import threading
import logging
from concurrent.futures import ThreadPoolExecutor
from dnslib import RR, QTYPE, A, DNSRecord
from dnslib.server import DNSServer, BaseResolver, DNSLogger,NoopDNSLogger

logger = logging.getLogger(__name__)

class LocalDNSResolver(BaseResolver):
    def __init__(self, protected_resources):
        """
        Resolver cục bộ cho phép phân giải các tài nguyên được bảo vệ và chuyển tiếp
        các yêu cầu khác đến DNS server ngoài (DNS forwarding).

        :param protected_resources: danh sách các tài nguyên được bảo vệ (dạng tên miền).
        """
        self.protected_resources = protected_resources
        # Use a consistent way to assign CGNAT IPs
        self.resource_ip_map = {}
        self.ip_counter = 1
        self.current_ip_index = 1
        self.cgnat_base_ip = '100.64.0.0'
        self.forwarding_dns_server = '8.8.8.8'  # DNS server ngoài để chuyển tiếp yêu cầu
        self.executor = ThreadPoolExecutor(max_workers=10)  # Tạo executor cho DNS forwarding

    def get_cgnat_ip(self):
        """
        Sinh một địa chỉ IP từ dải CGNAT cho tài nguyên được bảo vệ.
        """
        base_parts = [int(part) for part in self.cgnat_base_ip.split('.')]
        ip_int = (base_parts[0] << 24) + (base_parts[1] << 16) + (base_parts[2] << 8) + base_parts[3]
        ip_int += self.current_ip_index
        self.current_ip_index += 1
        new_ip = '.'.join(map(str, [
            (ip_int >> 24) & 0xFF,
            (ip_int >> 16) & 0xFF,
            (ip_int >> 8) & 0xFF,
            ip_int & 0xFF
        ]))
        return new_ip

    def forward_dns_request(self, domain):
        """
        Chuyển tiếp yêu cầu DNS đến DNS server bên ngoài trong một executor.

        :param domain: tên miền cần phân giải.
        :return: phản hồi từ DNS server bên ngoài hoặc None nếu không phân giải được.
        """
        try:
            forward_request = DNSRecord.question(domain)
            response = forward_request.send(self.forwarding_dns_server, 53, timeout=0.5)
            forward_reply = DNSRecord.parse(response)
            return forward_reply
        except Exception as e:
            logger.error(f"Failed to resolve {domain} via external DNS: {e}")
            return None

    def resolve(self, request, handler):
        """
        Xử lý yêu cầu DNS từ client.
        Nếu tên miền nằm trong danh sách tài nguyên được bảo vệ, trả về địa chỉ IP CGNAT.
        Nếu không, chuyển tiếp yêu cầu đến DNS server ngoài trong một executor.
        """
        reply = request.reply()
        qname = request.q.qname
        qtype = QTYPE[request.q.qtype]
        domain = str(qname).rstrip('.')

        if domain in self.protected_resources:
            if domain not in self.resource_ip_map:
                self.resource_ip_map[domain] = self.get_cgnat_ip()
            cgnat_ip = self.resource_ip_map[domain]
            # Nếu domain là tài nguyên được bảo vệ, trả về địa chỉ IP CGNAT
            reply.add_answer(RR(qname, QTYPE.A, rdata=A(cgnat_ip), ttl=60))
            logger.info(f"Resolved {domain} to {cgnat_ip} (CGNAT)")
            return reply
        else:
            # Nếu không phải là tài nguyên được bảo vệ, thực hiện DNS forwarding qua executor
            future = self.executor.submit(self.forward_dns_request, domain)
            forward_reply = future.result(timeout=1)  # Đợi kết quả tối đa trong 1 giây

            # Nếu có phản hồi từ DNS server ngoài, trả về kết quả đó
            if forward_reply:
                for rr in forward_reply.rr:
                    reply.add_answer(rr)
                # logger.info(f"Forwarded DNS request for {domain} to external DNS server")
            else:
                logger.warning(f"Could not resolve {domain} via external DNS server.")
            return reply

class DNSResolverThread(threading.Thread):
    def __init__(self, resolver):
        """
        Luồng khởi động DNS server cục bộ.
        
        :param resolver: resolver cục bộ sử dụng để xử lý các yêu cầu DNS.
        """
        super().__init__()
        self.resolver = resolver

    def run(self):
        logger_dns = NoopDNSLogger()
        dns_server = DNSServer(self.resolver, port=53, address='127.0.0.1', logger=logger_dns)
        dns_server.start_thread()
        logger.info("Local DNS server started on 127.0.0.1:53")
