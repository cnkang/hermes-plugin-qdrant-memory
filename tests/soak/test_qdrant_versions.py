"""Keep the Docker test server and installed client on the reviewed release pair."""

from pathlib import Path

from scripts.soak.prepare import QDRANT_IMAGE

ROOT = Path(__file__).resolve().parents[2]


def test_server_lanes_use_one_reviewed_qdrant_pair():
    """Version drift must fail CI instead of producing an ignored warning."""
    assert (
        QDRANT_IMAGE
        == "qdrant/qdrant@sha256:9bfc89f9914fc14876a88c05270685921a55b8877b254a98bcc1cc2f0a424cd2"
    )
    for name in ("ci", "shared-security"):
        text = (ROOT / f".github/workflows/{name}.yml").read_text()
        assert QDRANT_IMAGE in text
        assert "qdrant-client==1.20.0" in text
    soak = (ROOT / ".github/workflows/soak.yml").read_text()
    installs = [line for line in soak.splitlines() if "pip install '.[remote]'" in line]
    assert installs and all("qdrant-client==1.20.0" in line for line in installs)
    assert "error:Qdrant client version:UserWarning" in soak
