from ninja import Schema
from pydantic import Field


class RegisterIn(Schema):
    username: str = Field(min_length=1, max_length=150)
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=8, max_length=128)


class VerifyEmailIn(Schema):
    code: str = Field(pattern=r"^\d{6}$")


class LoginIn(Schema):
    username: str = Field(min_length=1, max_length=150)
    password: str = Field(min_length=1, max_length=128)


class NicknameIn(Schema):
    nickname: str = Field(min_length=1, max_length=50)


class UserOut(Schema):
    username: str
    nickname: str
    email: str
    avatar_url: str | None


class SessionOut(Schema):
    user: UserOut | None


class MessageOut(Schema):
    detail: str


class VerificationDispatchOut(MessageOut):
    resend_available_in: int
