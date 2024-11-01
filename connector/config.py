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

# Database configuration
DATABASE_URL = 'sqlite:///resources.db'

# Admin API configuration
ADMIN_API_HOST = '0.0.0.0'
ADMIN_API_PORT = 8000
ADMIN_API_KEY = '7C53ED1E212B4257B7B4C4B75BC75' #32 bytes test key

# curl.exe -X GET http://127.0.0.1:8000/resources -H "Authorization: Bearer 7C53ED1E212B4257B7B4C4B75BC75"
# curl.exe -X POST http://127.0.0.1:8000/resources -H "Authorization: Bearer 7C53ED1E212B4257B7B4C4B75BC75" -H "Content-Type: application/json" -d "{\"resource_id\": \"resource1\", \"name\": \"example.com\", \"protocol\": \"HTTP\", \"description\": \"Test resource\", \"ip\": \"192.168.1.1\", \"port\": 80}"
# curl.exe -X DELETE http://127.0.0.1:8000/resources/resource1 -H "Authorization: Bearer 7C53ED1E212B4257B7B4C4B75BC75"
