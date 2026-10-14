"""Request and response models for public authentication endpoints."""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _normalize_email(value: str) -> str:
    normalized = value.strip().lower()
    if "@" not in normalized or normalized.startswith("@") or normalized.endswith("@"):
        raise ValueError("A valid email address is required.")
    return normalized


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: str
    full_name: str
    is_email_verified: bool
    avatar_url: str | None = None


class UserUpdateRequest(BaseModel):
    full_name: str = Field(min_length=1, max_length=255)

    @field_validator("full_name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Full name is required.")
        return normalized


class RegisterRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=8, max_length=256)
    full_name: str = Field(min_length=1, max_length=255)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        return _normalize_email(value)

    @field_validator("full_name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Full name is required.")
        return normalized


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=256)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        return _normalize_email(value)


class EmailRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        return _normalize_email(value)


class OtpRequest(EmailRequest):
    code: str = Field(pattern=r"^\d{6}$")


class ResetPasswordRequest(OtpRequest):
    new_password: str = Field(min_length=8, max_length=256)


class TokenRefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=1)


class RegisterResponse(BaseModel):
    user: UserResponse


class AuthSessionResponse(BaseModel):
    access_token: str
    refresh_token: str
    user: UserResponse
