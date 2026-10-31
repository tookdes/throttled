import unittest
from unittest import mock

import mmio


class MmioModeTests(unittest.TestCase):
    def test_readonly_mapping_uses_readonly_fd_and_protection(self):
        mapping = mock.Mock()
        with mock.patch.object(mmio.os, "sysconf", return_value=4096):
            with mock.patch.object(mmio.os, "open", return_value=3) as open_fd:
                with mock.patch.object(mmio.os, "close"):
                    with mock.patch.object(mmio.mmap, "mmap", return_value=mapping) as mmap_call:
                        region = mmio.MMIO(0xFED159A0, 8, readonly=True)

        open_fd.assert_called_once_with("/dev/mem", mmio.os.O_RDONLY | mmio.os.O_SYNC)
        self.assertEqual(mmap_call.call_args.kwargs["prot"], mmio.mmap.PROT_READ)
        with self.assertRaises(mmio.MMIOError):
            region.write64(0, 0)
        region.close()

    def test_default_mapping_remains_read_write(self):
        mapping = mock.Mock()
        with mock.patch.object(mmio.os, "sysconf", return_value=4096):
            with mock.patch.object(mmio.os, "open", return_value=3) as open_fd:
                with mock.patch.object(mmio.os, "close"):
                    with mock.patch.object(mmio.mmap, "mmap", return_value=mapping) as mmap_call:
                        region = mmio.MMIO(0xFED159A0, 8)

        open_fd.assert_called_once_with("/dev/mem", mmio.os.O_RDWR | mmio.os.O_SYNC)
        self.assertEqual(
            mmap_call.call_args.kwargs["prot"],
            mmio.mmap.PROT_READ | mmio.mmap.PROT_WRITE,
        )
        region.close()


if __name__ == "__main__":
    unittest.main()
