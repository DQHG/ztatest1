# config.py
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

SIGNALING_SERVER_URL = 'wss://127.0.0.1:8080/ws'

# Đường dẫn đến chứng chỉ và khóa
SSL_CERT_FILE = os.path.join(BASE_DIR, 'certificates', 'connector.crt')
SSL_KEY_FILE = os.path.join(BASE_DIR, 'certificates', 'connector.key')
CA_CERT_FILE = os.path.join(BASE_DIR, 'certificates', 'ca.crt')

# Cấu hình logging
LOG_FILE = os.path.join(BASE_DIR, 'logs', 'connector.log')

# Cấu hình cơ sở dữ liệu
DATABASE_URL = 'sqlite:///resources.db'