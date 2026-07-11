#!/usr/bin/env python3
"""Sign an Experanto Edge release (server-side, holds the Ed25519 private key).

The agent (experanto_edge/update.py :: verify_manifest) verifies the produced
manifest, so keep the two in lockstep: the signature covers the ASCII bytes
"{version}:{sha256hex}", binding the requested version to the artifact content.

Usage
-----
  # one-time: generate the signing keypair
  python3 sign_release.py keygen --out-priv edge_signing.key
      -> writes the private key, prints the PUBLIC key (base64) to put in the
         agent config as `update_public_key`.

  # per release: sign the tarball -> manifest next to it
  python3 sign_release.py sign 0.2.0 dist/experanto-edge-0.2.0.tar.gz \
      --key edge_signing.key
      -> writes dist/experanto-edge-0.2.0.json

Serve the .tar.gz + .json under the agent's `update_base_url`. The private key
NEVER leaves the release machine; only the public key is distributed.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def keygen(args: argparse.Namespace) -> int:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    priv = Ed25519PrivateKey.generate()
    raw_priv = priv.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    raw_pub = priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    if os.path.exists(args.out_priv) and not args.force:
        print(f"esiste gia': {args.out_priv} (usa --force)", file=sys.stderr)
        return 1
    with open(os.open(args.out_priv, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as f:
        f.write(base64.b64encode(raw_priv))
    print(f"private key -> {args.out_priv} (chmod 600, NON committare)")
    print("public key (metti in config update_public_key):")
    print(base64.b64encode(raw_pub).decode())
    return 0


def sign(args: argparse.Namespace) -> int:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    with open(args.key, "rb") as f:
        raw_priv = base64.b64decode(f.read())
    priv = Ed25519PrivateKey.from_private_bytes(raw_priv)

    sha = _sha256(args.artifact)
    message = f"{args.version}:{sha}".encode("ascii")
    sig = base64.b64encode(priv.sign(message)).decode()

    manifest = {"version": args.version, "sha256": sha, "signature": sig}
    out = args.out or os.path.join(
        os.path.dirname(args.artifact),
        f"experanto-edge-{args.version}.json",
    )
    with open(out, "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"manifest -> {out}")
    print(json.dumps(manifest, indent=2))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="sign_release")
    sub = ap.add_subparsers(dest="cmd", required=True)

    kg = sub.add_parser("keygen", help="genera la coppia di chiavi Ed25519")
    kg.add_argument("--out-priv", default="edge_signing.key")
    kg.add_argument("--force", action="store_true")
    kg.set_defaults(fn=keygen)

    sg = sub.add_parser("sign", help="firma un artifact -> manifest")
    sg.add_argument("version")
    sg.add_argument("artifact")
    sg.add_argument("--key", required=True, help="file della private key (base64)")
    sg.add_argument("--out", help="path del manifest (default accanto all'artifact)")
    sg.set_defaults(fn=sign)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
