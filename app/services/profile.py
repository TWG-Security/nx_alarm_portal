"""Client profile of a site (the SOC, 2026-10-25): "more client-related site details", "which package the client is
on ... ground response option, while others pay for monitoring only ... so that we know to either send a ground
responder or not", and a response section on the alarm (call 911, call the client, Ajax).

sites.profile (migration 0035), every field optional:
  client_name, phone, contacts [{name, role, phone}], package ("ground_response" | "monitoring_only" | ""),
  police_phone (the police dispatch number for the site's area; the alarm's Call police button uses it, else 911),
  passcode, notes (access, pets, vehicles...), ajax_url (the client's page in Ajax PRO / Ajax web).
"""
import re

from pydantic import BaseModel, Field, field_validator

PACKAGES = {"ground_response": "Ground response", "monitoring_only": "Monitoring only"}
MAX_CONTACTS = 8
_PHONE_OK = re.compile(r"^[0-9+()\-.\s,;*#xX]*$")


def _phone(v: str) -> str:
    v = (v or "").strip()
    if not _PHONE_OK.match(v):
        raise ValueError("Phone numbers can only hold digits, spaces and + ( ) - . , ; * # x")
    return v


class Contact(BaseModel):
    name: str = Field(default="", max_length=120)
    role: str = Field(default="", max_length=80)
    phone: str = Field(default="", max_length=40)

    @field_validator("phone")
    @classmethod
    def _phone_ok(cls, v: str) -> str:
        return _phone(v)


class ProfileIn(BaseModel):
    client_name: str = Field(default="", max_length=200)
    phone: str = Field(default="", max_length=40)
    contacts: list[Contact] = Field(default_factory=list, max_length=MAX_CONTACTS)
    package: str = Field(default="", pattern="^(ground_response|monitoring_only|)$")
    police_phone: str = Field(default="", max_length=40)
    passcode: str = Field(default="", max_length=120)
    notes: str = Field(default="", max_length=4000)
    ajax_url: str = Field(default="", max_length=500)

    @field_validator("phone", "police_phone")
    @classmethod
    def _phones_ok(cls, v: str) -> str:
        return _phone(v)

    @field_validator("ajax_url")
    @classmethod
    def _https(cls, v: str) -> str:
        v = (v or "").strip()
        if v and not v.lower().startswith("https://"):
            raise ValueError("The Ajax link must start with https://")
        return v


def clean(p: ProfileIn) -> dict | None:
    d = p.model_dump()
    d["contacts"] = [c for c in d["contacts"] if c["name"].strip() or c["phone"].strip()]
    d = {k: (v.strip() if isinstance(v, str) else v) for k, v in d.items()}
    return d if any(d.values()) else None


def public(profile: dict | None) -> dict:
    """What pages get: the stored fields plus the package label."""
    p = dict(profile or {})
    p["package_label"] = PACKAGES.get(p.get("package", ""), "")
    return p
