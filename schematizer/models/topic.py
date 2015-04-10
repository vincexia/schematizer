# -*- coding: utf-8 -*-
from sqlalchemy import Column
from sqlalchemy import ForeignKey
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy import UniqueConstraint
from sqlalchemy.orm import relationship

from schematizer.models.avro_schema import AvroSchema
from schematizer.models.database import Base
from schematizer.models.types.time import build_time_column


class Topic(Base):

    __tablename__ = 'topic'
    __table_args__ = (
        UniqueConstraint(
            'topic',
            name='topic_unique_constraint'
        ),
    )

    id = Column(Integer, primary_key=True)

    # Topic name.
    topic = Column(String, nullable=False)

    # The associated domain_id for this topic.
    domain_id = Column(
        Integer,
        ForeignKey('domain.id'),
        nullable=False
    )

    avro_schemas = relationship(AvroSchema, backref="topic")

    # Timestamp when the entry is created
    created_at = build_time_column(
        default_now=True,
        nullable=False
    )

    # Timestamp when the entry is last updated
    updated_at = build_time_column(
        default_now=True,
        onupdate_now=True,
        nullable=False
    )

    def to_dict(self):
        topic_dict = {
            'topic_id': self.id,
            'topic': self.topic,
            'source': None if self.domain is None else self.domain.to_dict(),
            'created_at': self.created_at,
            'updated_at': self.updated_at
        }
        return topic_dict