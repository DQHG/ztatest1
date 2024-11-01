from .utils.database import SessionLocal, engine
from .utils.data_models import Resource as ResourceModel, Base
import logging

logger = logging.getLogger(__name__)

# Initialize the database
Base.metadata.create_all(bind=engine)

class ResourceManager:
    def __init__(self):
        pass  # We'll use context managers for sessions

    def list_resources(self):
        with SessionLocal() as db:
            resources = db.query(ResourceModel).all()
            return resources

    def get_resource_by_id(self, resource_id):
        with SessionLocal() as db:
            resource = db.query(ResourceModel).filter_by(resource_id=resource_id).first()
            return resource

    def get_resource_by_domain(self, domain):
        with SessionLocal() as db:
            resource = db.query(ResourceModel).filter_by(name=domain).first()
            return resource

    def add_resource(self, resource):
        with SessionLocal() as db:
            existing_resource = db.query(ResourceModel).filter_by(resource_id=resource.resource_id).first()
            if existing_resource:
                return False, "Resource ID already exists."
            db.add(resource)
            db.commit()
            return True, "Resource added successfully."

    def delete_resource_by_id(self, resource_id):
        with SessionLocal() as db:
            resource = db.query(ResourceModel).filter_by(resource_id=resource_id).first()
            if not resource:
                return False, "Resource not found."
            db.delete(resource)
            db.commit()
            return True, "Resource deleted successfully."

    def add_resource_from_dict(self, resource_data):
        try:
            resource = ResourceModel(**resource_data)
            return self.add_resource(resource)
        except Exception as e:
            logger.error(f"Error adding resource: {e}")
            return False, str(e)
