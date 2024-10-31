# data_models.py

from pydantic import BaseModel

class Resource(BaseModel):
    resource_id: str
    name: str
    protocol: str  # 'HTTP', 'HTTPS', 'SSH', 'RDP'
    description: str = None
    ip: str
    port: int
    username: str = None  # Dành cho SSH
