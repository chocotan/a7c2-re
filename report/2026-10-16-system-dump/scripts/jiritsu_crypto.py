"""AES-128-CTR used by libJiritsu's libCommuTalk.

Recovered from libJiritsu.so @0x1215c, which assembles the 16-byte key
material "jiritsu team\\0\\0\\0\\0" one byte at a time, derives the AES key with
MD5, sets the counter block to 00 01 02 ... 0f, and then calls
EVP_EncryptInit_ex(EVP_aes_128_ctr) plus EVP_EncryptUpdate over the buffer.

CTR is symmetric, so the same transform both protects and reveals content. A
file read through the f2=1 branch returns (file bytes XOR keystream); a read
through f2=0 returns the raw file bytes. The self-check on a real camera is to
read one file both ways: the XOR of the two replies must equal this keystream.
"""
import hashlib

KEY_MATERIAL = b"jiritsu team" + b"\x00" * 4
COUNTER_BLOCK = bytes(range(16))


def commu_key():
    """AES-128 key = MD5("jiritsu team" + four NUL bytes)."""
    return hashlib.md5(KEY_MATERIAL).digest()


def keystream(size, offset=0):
    """Keystream bytes for a CTR stream that always starts at a fresh context.

    The firmware creates a new context per call and never seeks, so byte i of a
    reply is covered by keystream byte i. ``offset`` exists only for paging long
    outputs in tests.
    """
    if not isinstance(size, int) or size < 0:
        raise ValueError("size must be a non-negative integer")
    if not isinstance(offset, int) or offset < 0:
        raise ValueError("offset must be a non-negative integer")
    if size == 0:
        return b""
    from Cryptodome.Cipher import AES
    initial_value = int.from_bytes(COUNTER_BLOCK, "big") + offset // 16
    cipher = AES.new(commu_key(), AES.MODE_CTR, nonce=b"", initial_value=initial_value)
    pad = b"\x00" * (offset % 16)
    stream = cipher.encrypt(pad + b"\x00" * size)
    return stream[offset % 16:]


def talk(data):
    """Apply the firmware's transform; identical in both directions."""
    if not isinstance(data, bytes):
        raise ValueError("data must be bytes")
    stream = keystream(len(data))
    return bytes(byte ^ mask for byte, mask in zip(data, stream))
