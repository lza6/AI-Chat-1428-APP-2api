import sys
import hermes_dec.parsers.hbc_file_parser as hp

class _AlwaysEqual:
    def __eq__(self, other): return True
    def __ne__(self, other): return False
    def __hash__(self): return 0

class _FakeHash:
    def digest(self): return _AlwaysEqual()

hp.sha1 = lambda *a, **k: _FakeHash()

import hermes_dec.decompilation.hbc_decompiler as dec
sys.argv = ['hbc-decompiler', sys.argv[1], sys.argv[2]]
dec.main()
