"""
Generate a self-signed HTTPS certificate so the MOBILE phone is allowed to use
its camera (browsers only expose the camera on https:// or localhost).

Run once:  python make_cert.py
Then start the HTTPS server (see start_https.bat / README).
The certificate covers localhost and your current LAN IP.
"""
import datetime
import ipaddress
import os
import socket

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

CERT_DIR = os.path.join(os.path.dirname(__file__), "certs")


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def main():
    os.makedirs(CERT_DIR, exist_ok=True)
    ip = lan_ip()
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "proctoring-demo")])
    san = x509.SubjectAlternativeName([
        x509.DNSName("localhost"),
        x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
        x509.IPAddress(ipaddress.ip_address(ip)),
    ])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.utcnow() - datetime.timedelta(days=1))
        .not_valid_after(datetime.datetime.utcnow() + datetime.timedelta(days=365))
        .add_extension(san, critical=False)
        .sign(key, hashes.SHA256())
    )

    with open(os.path.join(CERT_DIR, "key.pem"), "wb") as f:
        f.write(key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        ))
    with open(os.path.join(CERT_DIR, "cert.pem"), "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))

    print("Certificate created in ./certs for localhost + " + ip)
    print("Mobile URL will be:  https://%s:8443/mobile?session=exam-001" % ip)


if __name__ == "__main__":
    main()
