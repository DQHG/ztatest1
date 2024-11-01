import threading
import logging
from concurrent.futures import ThreadPoolExecutor
from dnslib import RR, QTYPE, A, DNSRecord
from dnslib.server import DNSServer, BaseResolver, NoopDNSLogger

logger = logging.getLogger(__name__)

class LocalDNSResolver(BaseResolver):
    def __init__(self, protected_resources):
        """
        Local resolver that handles resolution of protected resources and forwards other requests.

        :param protected_resources: List of protected resources (domain names).
        """
        self.protected_resources = protected_resources
        self.resource_ip_map = {}
        self.current_ip_index = 1
        self.cgnat_base_ip = '100.64.0.0'
        self.forwarding_dns_server = '8.8.8.8'  # External DNS server
        self.executor = ThreadPoolExecutor(max_workers=10)  # For DNS forwarding
        self.allocate_cgnat_ips()

    def allocate_cgnat_ips(self):
        """
        Allocate CGNAT IPs for each protected resource.
        """
        for resource in self.protected_resources:
            domain = resource.name
            if domain not in self.resource_ip_map:
                self.resource_ip_map[domain] = self.get_cgnat_ip()

    def update_resources(self, protected_resources):
        self.protected_resources = protected_resources
        self.allocate_cgnat_ips()

    def get_cgnat_ip(self):
        """
        Generate a CGNAT IP address for a protected resource.
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
        Forward DNS request to external DNS server.

        :param domain: Domain name to resolve.
        :return: DNS response or None if resolution fails.
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
        Handle DNS requests from the client.

        :param request: DNS request.
        :param handler: Request handler.
        :return: DNS response.
        """
        reply = request.reply()
        qname = request.q.qname
        qtype = QTYPE[request.q.qtype]
        domain = str(qname).rstrip('.')

        if domain in self.resource_ip_map:
            cgnat_ip = self.resource_ip_map[domain]
            # Respond with the CGNAT IP for protected resources
            reply.add_answer(RR(qname, QTYPE.A, rdata=A(cgnat_ip), ttl=60))
            logger.info(f"Resolved {domain} to {cgnat_ip} (CGNAT)")
            return reply
        else:
            # Forward other DNS requests
            future = self.executor.submit(self.forward_dns_request, domain)
            forward_reply = future.result(timeout=1)

            if forward_reply:
                for rr in forward_reply.rr:
                    reply.add_answer(rr)
            else:
                logger.warning(f"Could not resolve {domain} via external DNS server.")
            return reply

class DNSResolverThread(threading.Thread):
    def __init__(self, resolver):
        """
        Thread to start the local DNS server.

        :param resolver: Local DNS resolver.
        """
        super().__init__()
        self.resolver = resolver

    def run(self):
        logger_dns = NoopDNSLogger()
        dns_server = DNSServer(self.resolver, port=53, address='127.0.0.1', logger=logger_dns)
        dns_server.start_thread()
        logger.info("Local DNS server started on 127.0.0.1:53")
