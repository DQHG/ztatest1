# resource_manager.py

from .utils.database import SessionLocal, Base, engine
from .utils.data_models import Resource as ResourceModel
from sqlalchemy.orm import Session
import logging

logger = logging.getLogger(__name__)

# Khởi tạo cơ sở dữ liệu
Base.metadata.create_all(bind=engine)

class ResourceManager:
    def __init__(self):
        self.db = SessionLocal()

    def list_resources(self):
        resources = self.db.query(ResourceModel).all()
        return resources

    def add_resource(self, resource):
        existing_resource = self.db.query(ResourceModel).filter_by(resource_id=resource.resource_id).first()
        if existing_resource:
            return False, "Resource ID already exists."
        self.db.add(resource)
        self.db.commit()
        return True, "Resource added successfully."

    def update_resource(self, resource):
        existing_resource = self.db.query(ResourceModel).filter_by(resource_id=resource.resource_id).first()
        if not existing_resource:
            return False, "Resource not found."
        existing_resource.name = resource.name
        existing_resource.description = resource.description
        existing_resource.protocol = resource.protocol
        existing_resource.ip = resource.ip
        existing_resource.port = resource.port
        existing_resource.username = resource.username
        self.db.commit()
        return True, "Resource updated successfully."

    def delete_resource(self, resource_id):
        resource = self.db.query(ResourceModel).filter_by(resource_id=resource_id).first()
        if not resource:
            return False, "Resource not found."
        self.db.delete(resource)
        self.db.commit()
        return True, "Resource deleted successfully."

    def get_resource(self, resource_id):
        resource = self.db.query(ResourceModel).filter_by(resource_id=resource_id).first()
        return resource
