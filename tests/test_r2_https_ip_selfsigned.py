from pathlib import Path
import re

ROOT=Path(__file__).resolve().parents[1]
def test_ip_selfsigned_caddy_template_isolated():
    s=(ROOT/"deploy/Caddyfile.ip-selfsigned.example").read_text()
    stripped="\\n".join(line.split("#",1)[0] for line in s.splitlines())
    assert "auto_https disable_redirects" in stripped
    assert "https://{$R2_RELAY_IP}:8443" in stripped
    assert "tls internal" in stripped
    assert "reverse_proxy 127.0.0.1:18085" in stripped
    assert not re.search(r"(?m)^\\s*(?:http://|:443\\b|:80\\b)",stripped)
    assert "tls_insecure_skip_verify" not in stripped
    assert "https://:443" not in stripped

def test_ip_certificate_docs_require_client_validation():
    s=(ROOT/"docs/R2_HTTPS_IP_SELFSIGNED_CN.md").read_text()
    assert "IP Address" in s
    assert "root.crt" in s
    assert "SHA256" in s
    assert "verify=False" in s
