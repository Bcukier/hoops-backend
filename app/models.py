"""
Pydantic models for request/response validation.
"""
from pydantic import BaseModel, Field
from typing import Optional


# ── Auth ──────────────────────────────────────────────────────
class LoginRequest(BaseModel):
    email: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    player: "PlayerOut"


# ── Players ───────────────────────────────────────────────────
class PlayerCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., min_length=3, max_length=254)
    mobile: str = Field(default="", max_length=20)
    password: Optional[str] = Field(default=None, min_length=4, max_length=128)
    notif_pref: str = "email"   # comma-separated: "email", "sms", "email,sms", "none"


class PlayerOut(BaseModel):
    id: int
    name: str
    email: str
    mobile: str
    role: str
    priority: str
    status: str
    notif_pref: str
    force_password_change: bool = False
    created_at: str


class PlayerOutPublic(BaseModel):
    """Public-facing player info (no priority exposed)."""
    id: int
    name: str


class PlayerUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=100)
    email: Optional[str] = Field(default=None, max_length=254)
    mobile: Optional[str] = Field(default=None, max_length=20)
    password: Optional[str] = Field(default=None, min_length=4, max_length=128)
    notif_pref: Optional[str] = None


class PlayerAdminUpdate(BaseModel):
    priority: Optional[str] = None
    role: Optional[str] = None
    status: Optional[str] = None


class PlayerImportRow(BaseModel):
    name: str
    email: str
    mobile: str = ""


# ── Games ─────────────────────────────────────────────────────
class GameCreate(BaseModel):
    date: str  # ISO datetime string
    location: str = Field(..., min_length=1, max_length=200)
    algorithm: str = "first_come"
    cap: int = Field(default=12, ge=2, le=100)
    cap_enabled: bool = True
    owner_added_player_ids: list[int] = []
    notify_future_at: Optional[str] = None
    random_high_auto: bool = True  # Random algo: True=auto-add high pri, False=notify first only


class BatchGameCreate(BaseModel):
    """Create multiple games at once with a single batch notification."""
    games: list[GameCreate] = Field(..., min_length=1, max_length=20)


class GameUpdate(BaseModel):
    """Edit an existing game (organizer only)."""
    date: Optional[str] = None
    location: Optional[str] = Field(default=None, max_length=200)


class GameOut(BaseModel):
    id: int
    date: str
    location: str
    algorithm: str
    cap: int
    cap_enabled: bool
    created_by: int
    created_at: str
    notified_at: Optional[str]
    phase: str
    selection_done: bool
    closed: bool
    auto_selection_at: Optional[str] = None
    notify_standard_at: Optional[str] = None
    notify_low_at: Optional[str] = None
    notify_standard_status: Optional[str] = None
    notify_low_status: Optional[str] = None
    batch_id: Optional[str] = None
    random_high_auto: bool = True
    signups: list["SignupOut"] = []


class SignupOut(BaseModel):
    id: int
    player_id: int
    player_name: str
    signed_up_at: str
    status: str
    owner_added: bool


# ── Locations ────────────────────────────────────────────────
class LocationOut(BaseModel):
    id: int
    name: str
    address: str = ""
    sort_order: int = 0


class LocationCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    address: str = Field(default="", max_length=500)


class LocationUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=200)
    address: Optional[str] = Field(default=None, max_length=500)


class LocationReorder(BaseModel):
    """List of location IDs in the desired order."""
    location_ids: list[int]


# ── Settings ──────────────────────────────────────────────────
class SettingsOut(BaseModel):
    default_cap: int
    cap_enabled: bool
    default_algorithm: str
    default_location: str = ""
    high_priority_delay_minutes: int
    alternative_delay_minutes: int
    random_wait_period_minutes: int
    notify_owner_new_signup: bool
    locations: list[LocationOut]


class SettingsUpdate(BaseModel):
    default_cap: Optional[int] = Field(default=None, ge=2, le=100)
    cap_enabled: Optional[bool] = None
    default_algorithm: Optional[str] = None
    default_location: Optional[str] = None
    high_priority_delay_minutes: Optional[int] = Field(default=None, ge=0, le=10080)
    alternative_delay_minutes: Optional[int] = Field(default=None, ge=0, le=10080)
    random_wait_period_minutes: Optional[int] = Field(default=None, ge=0, le=10080)
    notify_owner_new_signup: Optional[bool] = None


# ── Notifications ─────────────────────────────────────────────
class NotificationOut(BaseModel):
    id: int
    game_id: int
    player_id: int
    notification_type: str
    channel: str
    message: str
    sent_at: str
    delivered: bool
