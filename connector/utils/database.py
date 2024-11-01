# database.py

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from ..config import DATABASE_URL
from sqlalchemy.ext.declarative import declarative_base

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False}  # Chỉ cần thiết với SQLite
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


