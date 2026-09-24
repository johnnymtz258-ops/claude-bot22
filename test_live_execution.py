import base64, json, tempfile, unittest
from pathlib import Path
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from live_execution import generate_keypair, load_keypair, sign_solana_transaction, wallet_address

class T(unittest.TestCase):
    def test_generate_and_sign_v0_transaction(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/"wallet.json"
            generate_keypair(p)
            key,pub=load_keypair(p)
            # one-signature synthetic v0 message, no instructions/lookups
            msg=bytes([0x80,1,0,0,1])+pub+(b"\x02"*32)+bytes([0,0])
            tx=bytes([1])+(b"\x00"*64)+msg
            signed=base64.b64decode(sign_solana_transaction(base64.b64encode(tx).decode(),p))
            sig=signed[1:65]
            Ed25519PublicKey.from_public_bytes(pub).verify(sig,msg)
            self.assertTrue(wallet_address(p))

    def test_bad_keypair_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/"bad.json"; p.write_text(json.dumps([1]*64))
            with self.assertRaises(ValueError): load_keypair(p)

if __name__=="__main__": unittest.main()
