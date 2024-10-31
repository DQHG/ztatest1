import os

SIGNALING_SERVER_HOST = '127.0.0.1'
SIGNALING_SERVER_PORT = 8080

# Đường dẫn đến chứng chỉ và khóa
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SSL_CERT_FILE = os.path.join(BASE_DIR, 'certificates', 'signaling_server.crt')
SSL_KEY_FILE = os.path.join(BASE_DIR, 'certificates', 'signaling_server.key')
CA_CERT_FILE = os.path.join(BASE_DIR, 'certificates', 'ca.crt')

# Cấu hình logging
LOG_FILE = os.path.join(BASE_DIR, 'logs', 'signaling_server.log')