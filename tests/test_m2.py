import os
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from storage.layouts import RowLayout
from storage.object_store import ObjectStore


class ObjectStoreTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def test_large_document_via_table_reference_after_reopen(self):
        data = bytes(range(256)) * 80
        store = ObjectStore(self.root / "objects")
        with RowLayout(self.root / "docs.db", ["title", "object_id"]) as docs:
            docs.insert(("Example", store.put(data)))
            docs.insert(("Empty", store.put(b"")))
        with RowLayout(self.root / "docs.db") as docs:
            reopened = ObjectStore(self.root / "objects")
            self.assertEqual(reopened.get(docs.get_row(0)[1]), data)
            self.assertEqual(reopened.get(docs.get_row(1)[1]), b"")

    def test_hook_failure_does_not_publish_an_object(self):
        def fail(_):
            raise RuntimeError("test failure")
        store = ObjectStore(self.root, encrypt=fail, decrypt=fail)
        with self.assertRaises(RuntimeError):
            store.put(b"document")
        with self.assertRaises(ValueError):
            store.get("../outside")
        with self.assertRaises(FileNotFoundError):
            store.get(uuid4().hex)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_authenticated_encryption_hooks(self):
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        except ImportError:
            self.skipTest("Install cryptography to run the AES-GCM integration test")
        cipher = AESGCM(AESGCM.generate_key(bit_length=128))

        def encrypt(data):
            nonce = os.urandom(12)
            return nonce + cipher.encrypt(nonce, data, None)

        def decrypt(data):
            return cipher.decrypt(data[:12], data[12:], None)

        data = b"example document contents " * 500
        key = ObjectStore(self.root, encrypt=encrypt, decrypt=decrypt).put(data)
        self.assertNotIn(data[:128], (self.root / (key + ".db")).read_bytes())
        with self.assertRaises(ValueError):
            ObjectStore(self.root).get(key)
        self.assertEqual(ObjectStore(self.root, encrypt=encrypt, decrypt=decrypt).get(key), data)
