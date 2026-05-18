from pydantic import BaseModel, ConfigDict
from datetime import datetime

class AppBaseModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)

class TimestampSchema(AppBaseModel):
    created_at: datetime
    updated_at: datetime
