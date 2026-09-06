from backend.app.core.security import create_access_token, decode_access_token, hash_password, verify_password


def test_password_hashing_and_tokens() -> None:
    password_hash = hash_password("StrongPass123!")
    assert password_hash != "StrongPass123!"
    assert verify_password("StrongPass123!", password_hash)
    assert not verify_password("wrong", password_hash)
    claims = decode_access_token(create_access_token("7", "CUSTOMER"))
    assert claims["sub"] == "7"
    assert claims["role"] == "CUSTOMER"
