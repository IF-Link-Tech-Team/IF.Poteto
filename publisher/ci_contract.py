"""A CI artifact credential authorizes one result, never a deployment action."""
import hashlib
import hmac


def credential(key, identifier, expires):
    message = f'poteto-ci-result/1:{identifier}:{expires}'.encode()
    return str(expires) + '.' + hmac.new(key, message, hashlib.sha256).hexdigest()
