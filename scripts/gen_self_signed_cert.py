"""自签 TLS 证书生成脚本 (v1.4 HTTPS 一键启用)

用法: python scripts/gen_self_signed_cert.py [--dir certs] [--days 365] [--cn 服务器IP或域名]
生成: <dir>/server.crt + <dir>/server.key，并在 <dir>/config-add.txt 输出
config.yaml 待追加的 HTTPS 配置段（server.ssl_certfile/ssl_keyfile）。

实现: 优先 cryptography 库；不可用时回退系统 openssl（麒麟/统信/Git 均自带）。
仅用于等保 2.0 三级"通信传输加密"最低要求；生产环境建议替换为 CA 签发证书。
"""
import argparse
import subprocess
import sys
from pathlib import Path


def gen_with_cryptography(out_dir: Path, days: int, cn: str) -> bool:
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError:
        return False
    import datetime
    import ipaddress

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    san = []
    try:
        san.append(x509.IPAddress(ipaddress.ip_address(cn)))
    except ValueError:
        san.append(x509.DNSName(cn))
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)  # 自签
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=days))
        .add_extension(x509.SubjectAlternativeName(san), critical=False)
        .sign(key, hashes.SHA256())
    )
    (out_dir / "server.key").write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        ))
    (out_dir / "server.crt").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return True


def gen_with_openssl(out_dir: Path, days: int, cn: str) -> None:
    cmd = [
        "openssl", "req", "-x509", "-newkey", "rsa:2048",
        "-keyout", str(out_dir / "server.key"),
        "-out", str(out_dir / "server.crt"),
        "-days", str(days), "-nodes",
        "-subj", f"/CN={cn}",
        "-addext", f"subjectAltName=IP:{cn}" if cn.replace(".", "").isdigit()
        else f"subjectAltName=DNS:{cn}",
    ]
    print("调用系统 openssl 生成证书...")
    subprocess.run(cmd, check=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="生成自签 TLS 证书（HTTPS 一键启用）")
    ap.add_argument("--dir", default="certs", help="证书输出目录（默认 ./certs）")
    ap.add_argument("--days", type=int, default=365, help="有效期天数（默认 365）")
    ap.add_argument("--cn", default="127.0.0.1", help="证书 CN/IP（默认 127.0.0.1）")
    args = ap.parse_args()

    out_dir = Path(args.dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if gen_with_cryptography(out_dir, args.days, args.cn):
        print(f"[cryptography] 证书已生成: {out_dir / 'server.crt'}")
    else:
        gen_with_openssl(out_dir, args.days, args.cn)
        print(f"[openssl] 证书已生成: {out_dir / 'server.crt'}")

    hint = (
        "\n# ---- HTTPS (v1.4 等保 2.0 通信加密) ----\n"
        "server:\n"
        f"  ssl_certfile: {out_dir / 'server.crt'}\n"
        f"  ssl_keyfile: {out_dir / 'server.key'}\n"
    )
    (out_dir / "config-add.txt").write_text(hint, encoding="utf-8")
    print("将以下内容追加到 config.yaml 后重启服务即启用 HTTPS：")
    print(hint)
    return 0


if __name__ == "__main__":
    sys.exit(main())
