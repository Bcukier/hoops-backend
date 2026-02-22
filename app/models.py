"""Pydantic models for request/response validation."""
from pydantic import BaseModel, Field
from typing import Optional


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
    notif_pref: str = "email"

class SignupRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., min_length=3, max_length=254)
    mobile: str = Field(default="", max_length=20)
    password: str = Field(..., min_length=4, max_length=128)
    notif_pref: str = "email"
    # Group join options — exactly one should be set
    join_group_name: str = ""
    join_organizer_email: str = ""
    create_group_name: str = ""

class PlayerOut(BaseModel):
    id: int
    name: str
    email: str
    mobile: str
    role: str          # "owner" if organizer of any group, else "player"
    priority: str      # legacy global — real priority is per-group
    status: str
    notif_pref: str
    force_password_change: bool = False
    is_superuser: bool = False
    email_verified: bool = False
    created_at: str
    groups: list["GroupMembershipOut"] = []
    pending_invitations: list["InvitationOut"] = []

class PlayerOutPublic(BaseModel):
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

# ── Groups ────────────────────────────────────────────────────
class GroupOut(BaseModel):
    id: int
    name: str
    my_role: str = "player"
    member_count: int = 0

class GroupMembershipOut(BaseModel):
    group_id: int
    group_name: str
    role: str         # "organizer" or "player"
    priority: str
    status: str       # "active", "pending", "invited"

class GroupMemberOut(BaseModel):
    player_id: int
    name: str
    email: str
    mobile: str
    role: str
    priority: str
    status: str

class InvitationOut(BaseModel):
    id: int
    group_id: int
    group_name: str
    invited_by_name: str
    token: str = ""

class GroupJoinRequest(BaseModel):
    group_name: str = ""
    organizer_email: str = ""

class GroupCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)

# ── Games ─────────────────────────────────────────────────────
class GameCreate(BaseModel):
    group_id: Optional[int] = None
    date: str
    location: str = Field(..., min_length=1, max_length=200)
    algorithm: str = "first_come"
    cap: int = Field(default=12, ge=2, le=100)
    cap_enabled: bool = True
    owner_added_player_ids: list[int] = []
    notify_future_at: Optional[str] = None
    random_high_auto: bool = True

class BatchGameCreate(BaseModel):
    games: list[GameCreate] = Field(..., min_length=1, max_length=20)

class GameUpdate(BaseModel):
    date: Optional[str] = None
    location: Optional[str] = Field(default=None, max_length=200)

class GameOut(BaseModel):
    id: int
    group_id: int = 0
    group_name: str = ""
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

# ── Locations ─────────────────────────────────────────────────
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
    notify_owner_player_drop: bool
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
    notify_owner_player_drop: Optional[bool] = None
