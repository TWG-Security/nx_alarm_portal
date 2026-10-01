"""A minimal software passkey (WebAuthn authenticator) for tests: P-256, "none" attestation.

It builds the same JSON a browser sends (see app/static/js/webauthn.js), so the server's real
py_webauthn verification runs end to end.
"""
import hashlib
import json
import os
import struct

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url


class SoftPasskey:
    def __init__(self, rp_id: str, origin: str):
        self.rp_id, self.origin = rp_id, origin
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.cred_id = os.urandom(32)
        self.user_handle = b""
        self.counter = 0

    def _cose_key(self) -> bytes:
        n = self.key.public_key().public_numbers()
        return cbor2.dumps({1: 2, 3: -7, -1: 1, -2: n.x.to_bytes(32, "big"), -3: n.y.to_bytes(32, "big")})

    def _client_data(self, kind: str, challenge: str, origin: str | None = None) -> bytes:
        return json.dumps({"type": kind, "challenge": challenge, "origin": origin or self.origin,
                           "crossOrigin": False}).encode()

    def register(self, options: dict, origin: str | None = None) -> dict:
        self.user_handle = base64url_to_bytes(options["user"]["id"])
        rp_hash = hashlib.sha256(self.rp_id.encode()).digest()
        attested = bytes(16) + struct.pack(">H", len(self.cred_id)) + self.cred_id + self._cose_key()
        auth_data = rp_hash + bytes([0x45]) + struct.pack(">I", self.counter) + attested   # UP | UV | AT
        att = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
        cd = self._client_data("webauthn.create", options["challenge"], origin)
        cid = bytes_to_base64url(self.cred_id)
        return {"id": cid, "rawId": cid, "type": "public-key", "clientExtensionResults": {},
                "response": {"clientDataJSON": bytes_to_base64url(cd), "attestationObject": bytes_to_base64url(att),
                             "transports": ["internal"]}}

    def sign(self, options: dict, origin: str | None = None) -> dict:
        self.counter += 1
        rp_hash = hashlib.sha256(self.rp_id.encode()).digest()
        auth_data = rp_hash + bytes([0x05]) + struct.pack(">I", self.counter)               # UP | UV
        cd = self._client_data("webauthn.get", options["challenge"], origin)
        sig = self.key.sign(auth_data + hashlib.sha256(cd).digest(), ec.ECDSA(hashes.SHA256()))
        cid = bytes_to_base64url(self.cred_id)
        return {"id": cid, "rawId": cid, "type": "public-key", "clientExtensionResults": {},
                "response": {"clientDataJSON": bytes_to_base64url(cd), "authenticatorData": bytes_to_base64url(auth_data),
                             "signature": bytes_to_base64url(sig), "userHandle": bytes_to_base64url(self.user_handle)}}
