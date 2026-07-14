from typing import Optional
from pydantic import BaseModel, Field

class BaseResultModel():
    def __init__(self, code=200, msg="success", data=None):
        self.code = code
        self.msg = msg
        self.data = data
