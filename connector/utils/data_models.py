# data_models.py

from sqlalchemy import Column, String, Text, Integer
from sqlalchemy.ext.declarative import declarative_base
from pydantic import BaseModel

Base = declarative_base()

class Resource(Base):
    __tablename__ = 'resources'
    resource_id = Column(String, primary_key=True, index=True)
    name = Column(String, index=True)
    protocol = Column(String)  # 'HTTP', 'HTTPS', 'SSH', 'RDP'
    description = Column(Text, nullable=True)
    ip = Column(String)
    port = Column(Integer)
    username = Column(String, nullable=True)  # Dành cho SSH

    def dict(self):
        return {
            'resource_id': self.resource_id,
            'name': self.name,
            'protocol': self.protocol,
            'description': self.description,
            'ip': self.ip,
            'port': self.port,
            'username': self.username
        }

# Pydantic model cho xác thực dữ liệu
class ResourceCreate(BaseModel):
    resource_id: str
    name: str
    protocol: str
    description: str = None
    ip: str
    port: int
    username: str = None  # Dành cho SSH
