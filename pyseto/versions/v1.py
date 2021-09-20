import hashlib
import hmac
from secrets import token_bytes
from typing import Any, Union

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey, RSAPublicKey
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import (
    load_der_private_key,
    load_der_public_key,
)

from ..exceptions import DecryptError, EncryptError, SignError, VerifyError
from ..key_interface import KeyInterface
from ..key_nist import NISTKey
from ..utils import base64url_decode, base64url_encode, pae


class V1Local(NISTKey):
    """
    The key object for v1.local.
    """

    _VERSION = 1
    _TYPE = "local"

    def __init__(self, key: Union[str, bytes]):

        super().__init__(key)
        return

    def encrypt(
        self,
        payload: bytes,
        footer: bytes = b"",
        implicit_assertion: bytes = b"",
        nonce: bytes = b"",
    ) -> bytes:

        if nonce:
            if len(nonce) != 32:
                raise ValueError("nonce must be 32 bytes long.")
        else:
            nonce = token_bytes(32)

        n = self._generate_hash(nonce, payload, 32)
        e = HKDF(
            algorithm=hashes.SHA384(),
            length=32,
            salt=n[0:16],
            info=b"paseto-encryption-key",
        )
        a = HKDF(
            algorithm=hashes.SHA384(),
            length=32,
            salt=n[0:16],
            info=b"paseto-auth-key-for-aead",
        )
        ek = e.derive(self._key)
        ak = a.derive(self._key)

        try:
            c = (
                Cipher(algorithms.AES(ek), modes.CTR(n[16:]))
                .encryptor()
                .update(payload)
            )
            pre_auth = pae([self.header, n, c, footer])
            t = hmac.new(ak, pre_auth, hashlib.sha384).digest()
            token = self._header + base64url_encode(n + c + t)
            if footer:
                token += b"." + base64url_encode(footer)
            return token
        except Exception as err:
            raise EncryptError("Failed to encrypt.") from err

    def decrypt(
        self, payload: bytes, footer: bytes = b"", implicit_assertion: bytes = b""
    ) -> bytes:

        n = payload[0:32]
        t = payload[-48:]
        c = payload[32 : len(payload) - 48]
        e = HKDF(
            algorithm=hashes.SHA384(),
            length=32,
            salt=n[0:16],
            info=b"paseto-encryption-key",
        )
        a = HKDF(
            algorithm=hashes.SHA384(),
            length=32,
            salt=n[0:16],
            info=b"paseto-auth-key-for-aead",
        )
        ek = e.derive(self._key)
        ak = a.derive(self._key)

        pre_auth = pae([self.header, n, c, footer])
        t2 = hmac.new(ak, pre_auth, hashlib.sha384).digest()
        if t != t2:
            raise DecryptError("Failed to decrypt.")

        try:
            return Cipher(algorithms.AES(ek), modes.CTR(n[16:])).decryptor().update(c)
        except Exception as err:
            raise DecryptError("Failed to decrypt.") from err

    def to_paserk_id(self) -> str:
        h = "k1.lid."
        p = self.to_paserk()
        digest = hashes.Hash(hashes.SHA384())
        digest.update((h + p).encode("utf-8"))
        d = digest.finalize()
        return h + base64url_encode(d[0:33]).decode("utf-8")


class V1Public(NISTKey):
    """
    The key object for v1.public.
    """

    _VERSION = 1
    _TYPE = "public"

    def __init__(self, key: Any):

        super().__init__(key)

        self._sig_size = 256
        if not isinstance(self._key, (RSAPublicKey, RSAPrivateKey)):
            raise ValueError("The key is not RSA key.")

        self._padding = padding.PSS(mgf=padding.MGF1(hashes.SHA384()), salt_length=48)
        return

    @classmethod
    def from_paserk(cls, paserk: str, wrapping_key: bytes = b"") -> KeyInterface:

        frags = paserk.split(".")
        if frags[0] != "k1":
            raise ValueError(f"Invalid PASERK version: {frags[0]}.")

        if not wrapping_key:
            if len(frags) != 3:
                raise ValueError("Invalid PASERK format.")
            wrapped = base64url_decode(frags[2])
            if frags[1] == "public":
                return cls(load_der_public_key(wrapped))
            if frags[1] == "secret":
                return cls(load_der_private_key(wrapped, password=None))
            if frags[1] == "secret-wrap":
                raise ValueError(f"{frags[1]} needs wrapping_key.")
            raise ValueError(f"Invalid PASERK type: {frags[1]}.")

        # wrapped key
        if len(frags) != 4:
            raise ValueError("Invalid PASERK format.")
        if frags[2] != "pie":
            raise ValueError(f"Unknown wrapping algorithm: {frags[2]}.")

        if frags[1] == "secret-wrap":
            h = "k1.secret-wrap.pie."
            k = cls._decode_pie(h, wrapping_key, frags[3])
            return cls(load_der_private_key(k, password=None))
        raise ValueError(f"Invalid PASERK type: {frags[1]}.")

    def sign(
        self, payload: bytes, footer: bytes = b"", implicit_assertion: bytes = b""
    ) -> bytes:

        if isinstance(self._key, RSAPublicKey):
            raise ValueError("A public key cannot be used for signing.")
        m2 = pae([self.header, payload, footer])
        try:
            return self._key.sign(m2, self._padding, hashes.SHA384())
        except Exception as err:
            raise SignError("Failed to sign.") from err

    def verify(
        self, payload: bytes, footer: bytes = b"", implicit_assertion: bytes = b""
    ):

        if len(payload) <= self._sig_size:
            raise ValueError("Invalid payload.")

        sig = payload[-self._sig_size :]
        m = payload[: len(payload) - self._sig_size]
        k = self._key if isinstance(self._key, RSAPublicKey) else self._key.public_key()
        m2 = pae([self.header, m, footer])
        try:
            k.verify(sig, m2, self._padding, hashes.SHA384())
        except Exception as err:
            raise VerifyError("Failed to verify.") from err
        return m

    def to_paserk(self, wrapping_key: Union[bytes, str] = b"") -> str:
        if not wrapping_key:
            if isinstance(self._key, RSAPublicKey):
                pub = self._key.public_bytes(
                    encoding=serialization.Encoding.DER,
                    format=serialization.PublicFormat.SubjectPublicKeyInfo,
                )
                return "k1.public." + base64url_encode(pub).decode("utf-8")

            priv = self._key.private_bytes(
                encoding=serialization.Encoding.DER,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption(),
            )
            return "k1.secret." + base64url_encode(priv).decode("utf-8")

        if isinstance(self._key, RSAPublicKey):
            raise ValueError("Public key cannot be wrapped.")

        # wrapped key
        bkey = (
            wrapping_key
            if isinstance(wrapping_key, bytes)
            else wrapping_key.encode("utf-8")
        )
        h = "k1.secret-wrap.pie."
        priv = self._key.private_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
        return h + self._encode_pie(h, bkey, priv)

    def to_paserk_id(self) -> str:
        p = self.to_paserk()
        h = "k1.pid." if isinstance(self._key, RSAPublicKey) else "k1.sid."
        digest = hashes.Hash(hashes.SHA384())
        digest.update((h + p).encode("utf-8"))
        d = digest.finalize()
        return h + base64url_encode(d[0:33]).decode("utf-8")
