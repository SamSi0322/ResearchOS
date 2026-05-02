from pydantic import BaseModel


class MessageOut(BaseModel):
    message: str


class OkOut(BaseModel):
    ok: bool = True
