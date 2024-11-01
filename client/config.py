import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

SIGNALING_SERVER_URL = 'wss://3.27.123.138:8080/ws'
# SIGNALING_SERVER_URL = 'wss://127.0.0.1:8080/ws'


# Đường dẫn đến chứng chỉ và khóa
SSL_CERT_FILE = os.path.join(BASE_DIR, 'certificates', 'client.crt')
SSL_KEY_FILE = os.path.join(BASE_DIR, 'certificates', 'client.key')
CA_CERT_FILE = os.path.join(BASE_DIR, 'certificates', 'ca.crt')

# Cấu hình logging
LOG_FILE = os.path.join(BASE_DIR, 'logs', 'client.log')

# Danh sách tài nguyên được bảo vệ (có thể được lấy từ Connector)
PROTECTED_RESOURCES = ['protected.example.com','protected1.example.com']

# Dải địa chỉ CGNAT
CGNAT_NETWORK = '100.64.0.0/10'

