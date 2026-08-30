import unittest

from desktop.allocation import Allocation, AllocationValidationError, validate_allocation
from desktop.hardware import HostResources


def _host(**overrides) -> HostResources:
    defaults = dict(
        os_name="macOS",
        os_version="14.5",
        architecture="arm64",
        total_cpu=12,
        total_memory_bytes=32 * 1024**3,
        total_storage_bytes=500 * 1024**3,
    )
    defaults.update(overrides)
    return HostResources(**defaults)


class TestAllocationValidation(unittest.TestCase):
    def test_valid_allocation_passes(self):
        allocation = Allocation(
            allocated_cpu=6, allocated_memory_bytes=12 * 1024**3, allocated_storage_bytes=80 * 1024**3
        )
        validate_allocation(allocation, _host())  # must not raise

    def test_negative_cpu_rejected(self):
        allocation = Allocation(allocated_cpu=-1, allocated_memory_bytes=0, allocated_storage_bytes=0)
        with self.assertRaises(AllocationValidationError):
            validate_allocation(allocation, _host())

    def test_negative_memory_rejected(self):
        allocation = Allocation(allocated_cpu=0, allocated_memory_bytes=-1, allocated_storage_bytes=0)
        with self.assertRaises(AllocationValidationError):
            validate_allocation(allocation, _host())

    def test_negative_storage_rejected(self):
        allocation = Allocation(allocated_cpu=0, allocated_memory_bytes=0, allocated_storage_bytes=-1)
        with self.assertRaises(AllocationValidationError):
            validate_allocation(allocation, _host())

    def test_cpu_over_total_rejected(self):
        allocation = Allocation(allocated_cpu=13, allocated_memory_bytes=0, allocated_storage_bytes=0)
        with self.assertRaises(AllocationValidationError):
            validate_allocation(allocation, _host(total_cpu=12))

    def test_memory_over_total_rejected(self):
        allocation = Allocation(
            allocated_cpu=1, allocated_memory_bytes=33 * 1024**3, allocated_storage_bytes=0
        )
        with self.assertRaises(AllocationValidationError):
            validate_allocation(allocation, _host(total_memory_bytes=32 * 1024**3))

    def test_storage_over_total_rejected(self):
        allocation = Allocation(
            allocated_cpu=1, allocated_memory_bytes=0, allocated_storage_bytes=501 * 1024**3
        )
        with self.assertRaises(AllocationValidationError):
            validate_allocation(allocation, _host(total_storage_bytes=500 * 1024**3))

    def test_exactly_at_total_is_allowed(self):
        allocation = Allocation(
            allocated_cpu=12, allocated_memory_bytes=32 * 1024**3, allocated_storage_bytes=500 * 1024**3
        )
        validate_allocation(allocation, _host())  # must not raise

    def test_multiple_violations_all_reported(self):
        allocation = Allocation(allocated_cpu=-1, allocated_memory_bytes=-1, allocated_storage_bytes=-1)
        with self.assertRaises(AllocationValidationError) as ctx:
            validate_allocation(allocation, _host())
        message = str(ctx.exception)
        self.assertIn("allocated_cpu", message)
        self.assertIn("allocated_memory_bytes", message)
        self.assertIn("allocated_storage_bytes", message)


if __name__ == "__main__":
    unittest.main()
